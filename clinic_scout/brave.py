"""Optional: look up clinics that have no website on OpenStreetMap via the Brave Search API.

Only runs when BRAVE_API_KEY is set in .env. Without it, the step is skipped.
"""

import os
from urllib.parse import urlparse

import requests

from clinic_scout.hosts import listing_host
from clinic_scout.website_finder import ascii_words

SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
GENERIC_WORDS = {
    "the", "and", "of", "dental", "dentist", "dentists", "dentistry", "clinic", "clinics", "medical",
    "surgery", "practice", "health", "healthcare", "centre", "center", "care", "doctor", "doctors",
    "group", "family", "smile", "smiles", "orthodontics", "implant", "implants", "branch",
}


class BraveLookup:
    def __init__(self, http):
        self.http = http
        self.key = os.getenv("BRAVE_API_KEY", "").strip()
        self.enabled = bool(self.key)

    def find_website(self, name, city):
        """Return (url_or_None, searched). `searched` is False if the search didn't run.

        Disables itself on an auth or quota error.
        """
        if not self.enabled or not match_words(name, city):
            return None, False  # nothing we could match results against, so don't spend a query
        try:
            resp = self.http.get(
                SEARCH_URL,
                params={"q": f'"{name}" {city}', "count": 10},
                headers={"Accept": "application/json", "X-Subscription-Token": self.key},
                timeout=(10, 20), attempts=3,
            )
        except requests.RequestException as exc:
            print(f"  Brave search failed ({exc}); skipping this clinic.")
            return None, False
        if resp.status_code in (401, 402, 403, 422, 429):
            print(f"  Brave API returned HTTP {resp.status_code}; turning the Brave check off for this run.")
            self.enabled = False
            return None, False
        if not resp.ok:
            return None, False
        results = resp.json().get("web", {}).get("results", [])
        return pick_own_site(name, [r.get("url", "") for r in results], city), True


def _registered_host(url):
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def match_words(name, city=""):
    """Distinctive words of the clinic's name to look for in result domains ([] if none)."""
    city_words = set(ascii_words(city))
    words = [w for w in ascii_words(name) if len(w) >= 4 and w not in GENERIC_WORDS and w not in city_words]
    if words:
        return words
    squashed = "".join(ascii_words(name))  # e.g. "The Medical Centre" -> "themedicalcentre"
    return [squashed] if len(squashed) >= 4 else []  # [] for names written only in non-Latin script


def pick_own_site(name, urls, city=""):
    """Pick the first result whose domain contains a distinctive word of the clinic's name.

    Listing/directory sites and the city's own words don't count. The caller still
    verifies the page (postcode, phone, or name plus city) before trusting it.
    """
    words = match_words(name, city)
    if not words:
        return None
    for url in urls:
        try:
            host = _registered_host(url)
        except ValueError:
            continue
        if not host or listing_host(url):
            continue
        if any(w in host.replace("-", "") for w in words):
            return f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
    return None
