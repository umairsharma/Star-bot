"""Fetch a clinic's homepage (respecting robots.txt) and look for marketing weaknesses.

Only the clinic's own site is fetched. Facebook/Instagram/Google links are
detected in the HTML but never requested.
"""

import codecs
import datetime
import re
import time
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from clinic_scout.hosts import host_matches, listing_host
from clinic_scout.http_client import MAX_REDIRECTS, redirect_target

TIMEOUT = 10
SLOW_SECONDS = 3.0
MAX_BYTES = 2_000_000
MIN_VISIBLE_TEXT = 300  # less than this suggests content is rendered by JavaScript

WHATSAPP_HOSTS = ("wa.me", "whatsapp.com")
SOCIAL_HOSTS = ("facebook.com", "fb.com", "fb.me", "instagram.com", "instagr.am")
BOOKING_HOSTS = (
    "doctolib.fr", "doctolib.de", "doctolib.it", "calendly.com", "zocdoc.com", "practo.com",
    "setmore.com", "acuityscheduling.com", "simplybook.me", "simplybook.it", "booksy.com",
    "fresha.com", "cliniko.com", "janeapp.com", "nexhealth.com", "localmed.com", "dentally.co",
    "portal.dentally.co", "pabau.com", "appointy.com", "doctoralia.com", "doctoralia.es",
    "docplanner.com", "jameda.de", "patientaccess.com", "accurx.nhs.uk", "opencare.com",
    "healthengine.com.au", "hotdoc.com.au", "zenoti.com", "mindbodyonline.com", "vagaro.com",
    "youcanbook.me", "gettimely.com", "dentr.co.uk", "flexbooker.com", "square.site",
)
BOOKING_WORDS = re.compile(
    r"\b(book(ing)?|appointments?|schedule|reserv\w*|termin\w*|cita|rendez[- ]?vous|"
    r"prenot\w*|afspraak|randevu|agendar)\b",
    re.I,
)
# "© 2019", "© 2015-2024", and ranges ending in "present" or a two-digit year ("© 2015-24").
COPYRIGHT_YEAR = re.compile(
    r"(?:©|\(c\)|copyright)\s*(?:(?:19|20)\d{2}\s*[-–—]\s*)?((?:19|20)\d{2})"
    r"(\s*[-–—]\s*(?:present|now|today|current|\d{2}(?!\d)))?",
    re.I,
)
CHARSET_META = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([a-zA-Z0-9_.:-]+)""", re.I)
PARKED_WORDS = re.compile(
    r"domain (is|may be) for sale|buy this domain|this domain is parked|parked free|"
    r"domain has expired|future home of something quite cool",
    re.I,
)
META_PIXEL = re.compile(r"connect\.facebook\.net/[^\"']*fbevents\.js|fbq\(\s*['\"]init|facebook\.com/tr\?id=", re.I)
GOOGLE_ADS = re.compile(r"googleadservices\.com|googleads\.g\.doubleclick\.net|['\"]AW-\d{6,}", re.I)
TAG_MANAGER = re.compile(r"googletagmanager\.com/gtm\.js|['\"]GTM-[A-Z0-9]+", re.I)
# Bot-protection pages served instead of the real site (only checked on small pages).
CHALLENGE_PAGE = re.compile(
    r"sgcaptcha|cf[-_]chl|<title>just a moment|attention required! \| cloudflare|"
    r"_incapsula_resource|sucuri_cloudproxy|checking your browser|ddos protection by",
    re.I,
)
CHALLENGE_MAX_BYTES = 30_000

class Disallowed(Exception):
    pass


class ListingSite(Exception):
    """The URL (or a redirect) leads to a listing/social site we must not fetch."""


def _join(base, link):
    """urljoin that returns "" instead of raising on a malformed link."""
    try:
        return urljoin(base, link.strip())
    except ValueError:
        return ""


def _path(link):
    try:
        return urlparse(link).path
    except ValueError:
        return ""


def decode_body(body, resp):
    """Decode a page using the header charset, then <meta charset>, then UTF-8.

    Unknown charset names (e.g. "windows-874" on some systems) are skipped.
    """
    candidates = []
    if "charset=" in resp.headers.get("Content-Type", "").lower():
        candidates.append(resp.encoding)
    meta = CHARSET_META.search(body[:4096])
    if meta:
        candidates.append(meta.group(1).decode("ascii"))
    for encoding in candidates + ["utf-8"]:
        try:
            if encoding and codecs.lookup(encoding):
                return body.decode(encoding, errors="replace")
        except LookupError:
            continue
    return body.decode("utf-8", errors="replace")


def _meta_refresh_target(body, url):
    """Return the URL a small page redirects to via <meta http-equiv="refresh">, if any."""
    soup = BeautifulSoup(body, "html.parser")
    tag = soup.find("meta", attrs={"http-equiv": re.compile(r"^refresh$", re.I)})
    match = re.search(r"\d*\s*;\s*(?:url\s*=\s*)?['\"]?([^'\"\s]+)", (tag or {}).get("content", ""), re.I)
    return (_join(url, match.group(1)) or None) if match else None


def _fetch(http, robots, url):
    """GET url, following redirects by hand so robots.txt is checked on every hop.

    Returns (final_url, response, body_bytes, seconds). `seconds` counts only
    time spent waiting on the site, not our own rate-limit pauses.
    """
    seconds = 0.0
    for _ in range(MAX_REDIRECTS + 1):
        if listing_host(url):
            raise ListingSite(listing_host(url))
        if not robots.allowed(url):
            raise Disallowed(url)
        resp = http.get(url, timeout=TIMEOUT, attempts=2, allow_redirects=False, stream=True)
        if resp.is_redirect and resp.headers.get("Location"):
            seconds += time.monotonic() - resp.started_at
            url = redirect_target(url, resp.headers["Location"])
            resp.close()
            continue
        body = b""
        for chunk in resp.iter_content(64 * 1024):
            body += chunk
            if len(body) >= MAX_BYTES:
                break
        resp.close()
        seconds += time.monotonic() - resp.started_at
        if len(body) < CHALLENGE_MAX_BYTES and not CHALLENGE_PAGE.search(body.decode("latin-1")):
            target = _meta_refresh_target(body.decode("utf-8", errors="replace"), url)
            if target and target != url:
                url = target
                continue
        return url, resp, body, seconds
    raise requests.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects")


def _candidate_urls(website):
    website = website.strip().split(";")[0].strip()  # OSM sometimes lists several
    if re.match(r"^https?://", website, re.I):
        urls = [website]
        if website.lower().startswith("https://"):
            urls.append("http://" + website[8:])  # in case only the certificate is broken
        return urls
    return ["https://" + website, "http://" + website]


def fetch_homepage(http, robots, website):
    """Try https then http. Returns a dict with either `error` or the page details."""
    last_error = None
    for url in _candidate_urls(website):
        try:
            final_url, resp, body, seconds = _fetch(http, robots, url)
        except Disallowed:
            return {"blocked": "robots.txt disallows it"}
        except ListingSite as exc:
            return {"listing": str(exc)}
        except requests.RequestException as exc:
            last_error = exc
            continue
        except ValueError:  # malformed URL in the map data
            return {"error": "invalid URL"}
        return {"url": final_url, "resp": resp, "body": body, "seconds": seconds,
                "tls_error": isinstance(last_error, requests.exceptions.SSLError)}
    return {"error": _describe_error(last_error)}


def _describe_error(exc):
    if isinstance(exc, requests.exceptions.SSLError):
        return "broken HTTPS certificate"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timed out"
    if isinstance(exc, requests.exceptions.ConnectionError):
        text = str(exc).lower()
        if "name or service not known" in text or "nodename nor servname" in text or "getaddrinfo" in text \
                or "name resolution" in text:
            return "domain does not resolve"
        return "connection failed"
    return type(exc).__name__ if exc else "unknown error"


def _site_root(url):
    try:
        parts = urlparse(url if "//" in url else "https://" + url)
    except ValueError:
        return url
    return f"{parts.scheme}://{parts.netloc}/"


def check_website(http, robots, website, today=None):
    """Return {"issues": [(code, detail)], "info": [str], "url": final_url, "phone": str}.

    Issue codes are scored in scoring.py. `info` lists caveats that don't score.
    If the map's link is a dead deep link (e.g. an old /contact page), the site's
    homepage is checked instead.
    """
    info = []
    page = fetch_homepage(http, robots, website)
    status = page["resp"].status_code if "resp" in page else None
    root = _site_root(website)
    if status in (404, 410) and root.rstrip("/") != website.rstrip("/"):
        info.append(f"map link is broken (HTTP {status}); checked the homepage instead")
        website, page = root, fetch_homepage(http, robots, root)
        status = page["resp"].status_code if "resp" in page else None

    if "listing" in page:
        return {"issues": [("no_website", f"only a {page['listing']} page")], "info": info, "url": website}
    if "blocked" in page:
        return {"issues": [], "info": info + [f"not checked: {page['blocked']}"], "url": website}
    if "error" in page:
        return {"issues": [("website_dead", page["error"])], "info": info, "url": website}

    resp, url, body = page["resp"], page["url"], page["body"]
    if status in (401, 403, 429) or (status == 503 and resp.headers.get("cf-mitigated")) or (
            len(body) < CHALLENGE_MAX_BYTES and CHALLENGE_PAGE.search(body.decode("latin-1"))):
        return {"issues": [], "info": info + ["not checked: site shows a bot-protection page"], "url": url}
    if status >= 400:
        return {"issues": [("website_dead", f"HTTP {status}")], "info": info, "url": url}
    content_type = resp.headers.get("Content-Type", "text/html").lower()
    if "html" not in content_type:
        return {"issues": [], "info": info + ["not checked: homepage is not an HTML page"], "url": url}

    result = analyse_html(decode_body(body, resp), url, page["seconds"], page["tls_error"], today)
    result["info"] = info + result["info"]
    return result


def analyse_html(html, url, seconds, tls_error=False, today=None):
    """Score-relevant checks on an already-fetched homepage."""
    today = today or datetime.date.today()
    soup = BeautifulSoup(html, "html.parser")
    issues, info = [], []

    has_scripts = soup.find("script") is not None
    has_frames = soup.find(["frameset", "frame"]) is not None
    embeds = [_join(url, t.get("src") or t.get("action") or "")
              for t in soup.find_all(["iframe", "script", "form"])]
    iframes = [_join(url, t.get("src") or "") for t in soup.find_all("iframe")]
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    visible_text = (soup.body or soup).get_text(" ", strip=True)
    if PARKED_WORDS.search(text[:5000]):
        return {"issues": [("website_dead", "domain parked or for sale")], "info": [], "url": url}

    if urlparse(url).scheme != "https":
        issues.append(("no_https", "broken certificate" if tls_error else ""))
    if not soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)}):
        issues.append(("no_viewport", ""))
    if seconds > SLOW_SECONDS:
        issues.append(("slow", f"{seconds:.1f}s"))

    if has_frames or (len(visible_text) < MIN_VISIBLE_TEXT and (has_scripts or iframes)):
        how = "inside a frame" if has_frames or not has_scripts else "via JavaScript"
        info.append(f"partly checked: page content loads {how}, so booking, social, copyright and ad checks were skipped")
        return {"issues": issues, "info": info, "url": url}

    hrefs = [h for h in (_join(url, a["href"]) for a in soup.find_all("a", href=True)) if h]

    tel_links = [unquote(h[4:]).strip() for h in hrefs if h.lower().startswith("tel:")]
    has_tel = bool(tel_links)
    has_whatsapp = any(h.lower().startswith("whatsapp:") or host_matches(h, WHATSAPP_HOSTS) for h in hrefs)
    has_booking_platform = any(host_matches(u, BOOKING_HOSTS) for u in hrefs + embeds if u)
    has_booking_link = any(
        BOOKING_WORDS.search(a.get_text(" ", strip=True)) or BOOKING_WORDS.search(_path(a["href"]))
        for a in soup.find_all("a", href=True)
    )
    has_booking_form = any(BOOKING_WORDS.search(f.get_text(" ", strip=True) + " " + str(f.attrs))
                           for f in soup.find_all("form"))
    if not (has_tel or has_whatsapp or has_booking_platform or has_booking_link or has_booking_form):
        issues.append(("no_booking", ""))

    years = []
    for year, tail in COPYRIGHT_YEAR.findall(text):
        two_digit_end = re.search(r"\d{2}$", tail)
        if two_digit_end:
            year = 2000 + int(two_digit_end.group())  # "© 2015-24"
        elif tail:
            year = today.year  # "© 2015-present"
        if 1990 <= int(year) <= today.year + 1:
            years.append(int(year))
    if years and max(years) < today.year - 2:
        issues.append(("old_copyright", str(max(years))))

    if not (META_PIXEL.search(html) or GOOGLE_ADS.search(html)):
        if TAG_MANAGER.search(html):
            info.append("uses Google Tag Manager, so ad tags may be hidden")
        else:
            issues.append(("no_ads", ""))

    if not any(host_matches(u, SOCIAL_HOSTS) for u in hrefs + iframes if u):  # links or embedded widgets
        issues.append(("no_social", ""))

    return {"issues": issues, "info": info, "url": url, "phone": tel_links[0] if tel_links else ""}
