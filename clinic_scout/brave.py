"""Optional: look up clinics that have no website on OpenStreetMap via the Brave Search API.

Only runs when BRAVE_API_KEY is set in .env. Without it, the step is skipped.
"""

import os
import re
from urllib.parse import urlparse

import requests

from clinic_scout.website_checks import listing_host

SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
# Results on these sites are about the clinic, not the clinic's own website
# (on top of the social/directory sites in website_checks.LISTING_HOSTS).
DIRECTORY_HOSTS = (
    "healthgrades.com", "wikipedia.org", "192.com", "cylex-uk.co.uk", "cylex.de",
    "foursquare.com", "mapquest.com", "apple.com", "bing.com", "hotfrog.com", "nextdoor.com",
    "ratemds.com", "vitals.com", "webmd.com", "opencare.com", "trustpilot.com", "cqc.org.uk",
    "companieshouse.gov.uk", "company-information.service.gov.uk", "checkatrade.com",
    "openstreetmap.org", "waze.com", "pinterest.com", "glassdoor.com", "indeed.com",
)
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
        """Return the clinic's own website URL if search finds one, else None.

        Disables itself (and returns None) on an auth or quota error.
        """
        if not self.enabled:
            return None
        try:
            resp = self.http.get(
                SEARCH_URL,
                params={"q": f'"{name}" {city}', "count": 10},
                headers={"Accept": "application/json", "X-Subscription-Token": self.key},
                timeout=(10, 20), attempts=3,
            )
        except requests.RequestException as exc:
            print(f"  Brave search failed ({exc}); skipping this clinic.")
            return None
        if resp.status_code in (401, 402, 403, 422, 429):
            print(f"  Brave API returned HTTP {resp.status_code}; turning the Brave check off for this run.")
            self.enabled = False
            return None
        if not resp.ok:
            return None
        results = resp.json().get("web", {}).get("results", [])
        return pick_own_site(name, [r.get("url", "") for r in results])


def _registered_host(url):
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def pick_own_site(name, urls):
    """Pick the first result whose domain contains a distinctive word of the clinic's name."""
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) >= 4 and w not in GENERIC_WORDS]
    if not words:  # e.g. "The Medical Centre": fall back to the whole name squashed together
        words = ["".join(re.findall(r"[a-z0-9]+", name.lower()))]
    for url in urls:
        host = _registered_host(url)
        if not host or listing_host(url) or any(host == d or host.endswith("." + d) for d in DIRECTORY_HOSTS):
            continue
        squashed = host.replace("-", "")
        if any(w in squashed for w in words):
            return f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
    return None
