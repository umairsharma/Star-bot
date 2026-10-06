"""Polite HTTP: descriptive User-Agent, timeouts, retries, rate limits, robots.txt."""

import os
import re
import time
from urllib.parse import urljoin, urlparse

import requests

from clinic_scout import __version__
from clinic_scout.hosts import listing_host, site_key

PER_DOMAIN_DELAY = 1.0  # seconds between requests to the same domain
GLOBAL_DELAY = 0.3  # seconds between any two requests
RETRY_STATUSES = {429, 500, 502, 503, 504}
ROBOTS_AGENT = "clinic-scout"
MAX_REDIRECTS = 5


def user_agent():
    contact = os.getenv("CONTACT_EMAIL", "").strip()
    suffix = f"; {contact}" if contact else ""
    return f"clinic-scout/{__version__} (local-business research CLI{suffix})"


def redirect_target(url, location):
    """Absolute URL for a Location header. Raises requests.InvalidURL if it's malformed."""
    try:
        location = location.encode("latin-1").decode("utf-8")  # headers arrive as Latin-1
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    try:
        return urljoin(url, location.strip())
    except ValueError as exc:
        raise requests.exceptions.InvalidURL(f"bad redirect to {location!r}") from exc


class PoliteSession:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent()
        self._last_by_site = {}
        self._last_any = 0.0

    def _wait(self, url):
        site = site_key(url)
        now = time.monotonic()
        wait_until = max(
            self._last_any + GLOBAL_DELAY,
            self._last_by_site.get(site, 0.0) + PER_DOMAIN_DELAY,
        )
        if wait_until > now:
            time.sleep(wait_until - now)
        stamp = time.monotonic()
        self._last_any = stamp
        self._last_by_site[site] = stamp

    def request(self, method, url, timeout=10, attempts=1, **kwargs):
        """Send a request, retrying on network errors and 429/5xx with backoff.

        `resp.started_at` is the monotonic time the request was sent (after any
        rate-limit wait), so callers can time the response themselves.
        Raises the last exception if every attempt fails.
        """
        last_error = None
        for attempt in range(attempts):
            self._wait(url)
            started_at = time.monotonic()
            try:
                resp = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
            else:
                resp.started_at = started_at
                if resp.status_code not in RETRY_STATUSES or attempt == attempts - 1:
                    return resp
                last_error = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                retry_after = resp.headers.get("Retry-After", "")
                resp.close()
                if retry_after.isdigit():
                    time.sleep(min(int(retry_after), 60))
                    continue
            if attempt < attempts - 1:
                time.sleep(2 ** (attempt + 1))
        raise last_error

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


def parse_robots(text, agent=ROBOTS_AGENT):
    """Return the (allow, path_pattern) rules that apply to `agent` (RFC 9309).

    Groups naming our product token win over `*`; all matching groups are merged.
    """
    groups, in_agent_lines = [], False
    for raw in text.lstrip("﻿").splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if not in_agent_lines:
                groups.append({"agents": set(), "rules": []})
            groups[-1]["agents"].add(value.lower())
            in_agent_lines = True
        elif key in ("allow", "disallow"):
            if groups:
                groups[-1]["rules"].append((key == "allow", value))
            in_agent_lines = False
    ours = [g for g in groups if agent.lower() in g["agents"]]
    chosen = ours or [g for g in groups if "*" in g["agents"]]
    return [rule for g in chosen for rule in g["rules"]]


def _pattern_matches(pattern, path):
    regex = re.escape(pattern).replace(r"\*", ".*")
    if regex.endswith(r"\$"):
        regex = regex[:-2] + "$"
    return re.match(regex, path) is not None


def robots_allows(rules, url):
    """Longest matching rule wins; on a tie, allow wins. No matching rule means allowed."""
    parts = urlparse(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    best = None
    for allow, pattern in rules:
        if pattern and _pattern_matches(pattern, path):
            if best is None or len(pattern) > best[0] or (len(pattern) == best[0] and allow):
                best = (len(pattern), allow)
    return True if best is None else best[1]


ALLOW_ALL = []
DISALLOW_ALL = [(False, "/")]


class RobotsCache:
    """Fetches and caches robots.txt per site, following RFC 9309.

    4xx (no robots.txt) means everything is allowed; 5xx means nothing is.
    Redirects are followed by hand (at most 5), so every hop is rate-limited and
    listing sites like Facebook or Google are never requested.
    Network errors propagate so the caller can treat the site as unreachable.
    """

    def __init__(self, http):
        self.http = http
        self._rules = {}

    def _fetch_rules(self, root):
        url = root + "/robots.txt"
        for _ in range(MAX_REDIRECTS + 1):
            if listing_host(url):
                return ALLOW_ALL  # the page fetch refuses that host anyway
            resp = self.http.get(url, timeout=10, attempts=2, allow_redirects=False)
            if resp.is_redirect and resp.headers.get("Location"):
                url = redirect_target(url, resp.headers["Location"])
                resp.close()
                continue
            if resp.status_code >= 500:
                return DISALLOW_ALL
            if resp.status_code >= 400:
                return ALLOW_ALL
            return parse_robots(resp.text)
        return ALLOW_ALL  # too many redirects: RFC 9309 treats robots.txt as unavailable

    def allowed(self, url):
        parts = urlparse(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self._rules:
            self._rules[root] = self._fetch_rules(root)
        return robots_allows(self._rules[root], url)
