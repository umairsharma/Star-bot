"""Free website finder: guess a clinic's domain from its name, then verify the page is theirs.

No search engine or API key involved. Each guess is checked with a DNS lookup
first, so only domains that exist get an HTTP request (rate-limited, robots.txt
respected). A page only counts if it mentions the clinic's postcode, phone
number, or its name together with the city (whole words only).
"""

import re
import socket
import unicodedata

import requests
from bs4 import BeautifulSoup

from clinic_scout.website_checks import PARKED_WORDS, decode_body, fetch_homepage

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


def ascii_words(text):
    """Lower-case ASCII words, with accents removed ("Clínica Dental" -> ["clinica", "dental"])."""
    text = text.lower().replace("&", " and ").replace("'", "").replace("’", "").replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.findall(r"[a-z0-9]+", text)


def domain_guesses(name, country_code):
    """Likely domains for a clinic name, most likely first."""
    words = ascii_words(name)
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
    return " ".join(ascii_words(text))


def page_matches(text, clinic, city):
    """True if the page text clearly belongs to this clinic. All matches are whole words."""
    words = f" {_norm(text)} "

    def has(phrase):
        phrase = _norm(phrase)
        return bool(phrase) and f" {phrase} " in words

    city_named = has(city)
    postcode = _norm(clinic.get("postcode", ""))
    if len(postcode.replace(" ", "")) >= 4:
        found = has(postcode) or has(postcode.replace(" ", ""))
        # All-digit postcodes (e.g. "2000", "8001") also look like years and prices.
        if found and (city_named or not postcode.replace(" ", "").isdigit()):
            return True
    phone = re.sub(r"\D", "", clinic.get("phone", ""))[-9:]
    if len(phone) == 9 and phone in re.sub(r"\D", "", text):
        return True
    if city_named:
        name = re.sub(r"^the\s+", "", clinic["name"], flags=re.I)
        street = clinic.get("street", "")
        if has(name) or (len(_norm(street)) > 6 and has(street)):
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

    def _check(self, url, clinic, city):
        """Return ("match", final_url), ("other", None) if the site answered but isn't
        this clinic's, or ("unreachable", None)."""
        try:
            page = fetch_homepage(self.http, self.robots, url)
            if "resp" not in page:
                return "unreachable", None
            if page["resp"].status_code >= 400:
                return "other", None
            text = BeautifulSoup(decode_body(page["body"], page["resp"]), "html.parser").get_text(" ", strip=True)
        except (requests.RequestException, ValueError, LookupError):
            return "unreachable", None
        if PARKED_WORDS.search(text[:20000]) or not page_matches(text, clinic, city):
            return "other", None
        return "match", page["url"]

    def verify_url(self, url, clinic, city):
        """Return the final URL if the page at `url` is this clinic's own site, else None."""
        return self._check(url, clinic, city)[1]

    def find(self, clinic, city):
        """Return the clinic's own homepage URL, or None."""
        for domain in domain_guesses(clinic["name"], self.country_code):
            for host in (domain, "www." + domain):
                if self.use_dns and not self._resolves(host):
                    continue
                outcome, url = self._check(f"https://{host}/", clinic, city)
                if outcome == "match":
                    return url
                if outcome == "other":
                    break  # the site answered but isn't this clinic's; try the next domain
        return None
