"""Free website finder: guess a clinic's domain from its name, then verify the page is theirs.

No search engine or API key involved. Each guess is checked with a DNS lookup
first, so only domains that exist get an HTTP request (rate-limited, robots.txt
respected). A page only counts if it mentions the clinic's postcode, phone
number, or its name together with the city.
"""

import re
import socket

from bs4 import BeautifulSoup

from clinic_scout.website_checks import PARKED_WORDS, fetch_homepage

# Domain endings to try, by ISO country code. Anything else gets its ccTLD + .com.
COUNTRY_TLDS = {
    "gb": ["co.uk", "com", "uk"], "us": ["com"], "ca": ["ca", "com"], "au": ["com.au", "com"],
    "nz": ["co.nz", "com"], "ie": ["ie", "com"], "in": ["in", "co.in", "com"], "za": ["co.za", "com"],
    "jp": ["jp", "co.jp", "com"], "br": ["com.br", "com"], "mx": ["com.mx", "mx", "com"],
    "ar": ["com.ar", "com"], "tr": ["com.tr", "com"], "pk": ["pk", "com.pk", "com"],
    "ng": ["com.ng", "ng", "com"], "ae": ["ae", "com"], "sg": ["com.sg", "sg", "com"],
}
# Words often left out of a clinic's domain ("Ashley Down Dental Care" -> ashleydowndental.co.uk).
TRAILING_WORDS = {"care", "centre", "center", "practice", "clinic", "surgery", "ltd", "limited", "group", "uk"}
MAX_SLUGS = 4


def _words(name):
    name = name.lower().replace("&", " and ").replace("'", "").replace("’", "")
    return re.findall(r"[a-z0-9]+", name)


def domain_guesses(name, country_code):
    """Likely domains for a clinic name, most likely first."""
    words = _words(name)
    if not words:
        return []
    variants = [words]
    if words[0] == "the" and len(words) > 1:
        variants.append(words[1:])
    trimmed = [w for w in words if w != "the"]
    while len(trimmed) > 1 and trimmed[-1] in TRAILING_WORDS:
        trimmed = trimmed[:-1]
        variants.append(list(trimmed))
    slugs = []
    for v in variants:
        for slug in ("".join(v), "-".join(v)):
            if slug not in slugs and len(slug) >= 4:
                slugs.append(slug)
    tlds = COUNTRY_TLDS.get(country_code, [country_code, "com"] if country_code else ["com"])
    return [f"{slug}.{tld}" for slug in slugs[:MAX_SLUGS] for tld in tlds]


def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower().replace("&", " and "))).strip()


def page_matches(text, clinic, city):
    """True if the page text clearly belongs to this clinic."""
    flat = re.sub(r"\s+", "", text.lower())
    postcode = re.sub(r"\s+", "", clinic.get("postcode", "").lower())
    if len(postcode) >= 4 and postcode in flat:
        return True
    phone = re.sub(r"\D", "", clinic.get("phone", ""))[-9:]
    if len(phone) == 9 and phone in re.sub(r"\D", "", text):
        return True
    norm_text, norm_city = _norm(text), _norm(city)
    if norm_city and norm_city in norm_text:
        name = _norm(re.sub(r"^the\s+", "", clinic["name"], flags=re.I))
        street = _norm(clinic.get("street", ""))
        if (name and name in norm_text) or (len(street) > 6 and street in norm_text):
            return True
    return False


class WebsiteFinder:
    def __init__(self, http, robots, country_code):
        self.http = http
        self.robots = robots
        self.country_code = (country_code or "").lower()
        self.use_dns = self._resolves("openstreetmap.org")  # skip the DNS pre-check if DNS is unavailable

    @staticmethod
    def _resolves(host):
        try:
            socket.getaddrinfo(host, 443)
            return True
        except (socket.gaierror, UnicodeError, OSError):
            return False

    def find(self, clinic, city):
        """Return the clinic's own homepage URL, or None."""
        for domain in domain_guesses(clinic["name"], self.country_code):
            for host in (domain, "www." + domain):
                if self.use_dns and not self._resolves(host):
                    continue
                page = fetch_homepage(self.http, self.robots, f"https://{host}/")
                if "resp" not in page or page["resp"].status_code >= 400:
                    continue  # no usable page here; try the www. variant
                html = page["body"].decode(page["resp"].encoding or "utf-8", errors="replace")
                text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
                if not PARKED_WORDS.search(text[:20000]) and page_matches(text, clinic, city):
                    return page["url"]
                break  # a real page that isn't this clinic's; try the next domain
        return None
