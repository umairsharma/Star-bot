"""Polite HTTP: descriptive User-Agent, timeouts, retries, and rate limits."""

import os
import time
from urllib.parse import urlparse

import requests

from clinic_scout import __version__

PER_DOMAIN_DELAY = 1.0  # seconds between requests to the same domain
GLOBAL_DELAY = 0.3  # seconds between any two requests
RETRY_STATUSES = {429, 500, 502, 503, 504}


def user_agent():
    contact = os.getenv("CONTACT_EMAIL", "").strip()
    suffix = f"; {contact}" if contact else ""
    return f"clinic-scout/{__version__} (local-business research CLI{suffix})"


class PoliteSession:
    def __init__(self, retries=3):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent()
        self.retries = retries
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

    def request(self, method, url, timeout=10, retry=True, **kwargs):
        """Send a request, retrying on network errors and 429/5xx with backoff.

        Raises the last exception if every attempt fails.
        """
        attempts = self.retries + 1 if retry else 1
        last_error = None
        for attempt in range(attempts):
            self._wait(url)
            try:
                resp = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
            else:
                if resp.status_code not in RETRY_STATUSES or attempt == attempts - 1:
                    return resp
                last_error = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                retry_after = resp.headers.get("Retry-After", "")
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
