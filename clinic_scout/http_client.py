"""Polite HTTP: descriptive User-Agent, timeouts, retries, rate limits, robots.txt."""

import os
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

from clinic_scout import __version__

PER_DOMAIN_DELAY = 1.0  # seconds between requests to the same domain
GLOBAL_DELAY = 0.3  # seconds between any two requests
RETRY_STATUSES = {429, 500, 502, 503, 504}
ROBOTS_AGENT = "clinic-scout"


def user_agent():
    contact = os.getenv("CONTACT_EMAIL", "").strip()
    suffix = f"; {contact}" if contact else ""
    return f"clinic-scout/{__version__} (local-business research CLI{suffix})"


class PoliteSession:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent()
        self._last_by_domain = {}
        self._last_any = 0.0

    def _wait(self, url):
        domain = urlparse(url).netloc.lower()
        now = time.monotonic()
        wait_until = max(
            self._last_any + GLOBAL_DELAY,
            self._last_by_domain.get(domain, 0.0) + PER_DOMAIN_DELAY,
        )
        if wait_until > now:
            time.sleep(wait_until - now)
        stamp = time.monotonic()
        self._last_any = stamp
        self._last_by_domain[domain] = stamp

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


class RobotsCache:
    """Fetches and caches robots.txt per site, following RFC 9309.

    4xx (no robots.txt) means everything is allowed; 5xx means nothing is.
    Network errors propagate so the caller can treat the site as unreachable.
    """

    def __init__(self, http):
        self.http = http
        self._parsers = {}

    def allowed(self, url):
        parts = urlparse(url)
        root = f"{parts.scheme}://{parts.netloc}"
        parser = self._parsers.get(root)
        if parser is None:
            parser = RobotFileParser()
            resp = self.http.get(root + "/robots.txt", timeout=10)
            if resp.status_code >= 500:
                parser.disallow_all = True
            elif resp.status_code >= 400:
                parser.allow_all = True
            else:
                parser.parse(resp.text.splitlines())
            self._parsers[root] = parser
        return parser.can_fetch(ROBOTS_AGENT, url)
