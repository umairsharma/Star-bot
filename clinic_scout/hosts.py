"""Host helpers shared by the fetcher, robots.txt handling and the website finders.

LISTING_HOSTS are sites that are *about* a clinic (social pages, directories,
review sites, Google) rather than the clinic's own website. They are never
fetched, not even through a redirect or a robots.txt request.
"""

import re
from urllib.parse import parse_qs, urlparse

LISTING_HOSTS = (
    # Google and Meta, including their short links
    "facebook.com", "fb.com", "fb.me", "fb.watch", "m.me", "messenger.com", "instagram.com",
    "instagr.am", "ig.me", "goo.gl", "g.co", "g.page", "goo.gle", "share.google", "forms.gle",
    "business.site", "blogspot.com", "youtube.com", "youtu.be",
    # Other social sites
    "linkedin.com", "twitter.com", "x.com", "tiktok.com", "pinterest.com", "linktr.ee",
    "wa.me", "whatsapp.com",
    # Directories and review sites
    "www.nhs.uk", "yelp.com", "yelp.co.uk", "healthgrades.com", "zocdoc.com", "doctolib.fr",
    "doctolib.de", "doctolib.it", "doctoralia.com", "doctoralia.es", "practo.com", "jameda.de",
    "yell.com", "yellowpages.com", "tripadvisor.com", "wikipedia.org", "192.com", "cylex-uk.co.uk",
    "cylex.de", "foursquare.com", "mapquest.com", "apple.com", "bing.com", "hotfrog.com",
    "nextdoor.com", "ratemds.com", "vitals.com", "webmd.com", "opencare.com", "trustpilot.com",
    "cqc.org.uk", "companieshouse.gov.uk", "company-information.service.gov.uk", "checkatrade.com",
    "openstreetmap.org", "waze.com", "glassdoor.com", "indeed.com",
)
# google.com, google.co.uk, google.de, ... and anything under the .google TLD
GOOGLE_HOST = re.compile(r"(^|\.)google\.[a-z]{2,3}(\.[a-z]{2})?$|\.google$")


def host_of(url):
    """Lower-case ASCII host name of a URL (scheme optional), or "" if it can't be parsed.

    Internationalised names are converted to punycode, so "zahnarzt-müller.de" and
    "xn--zahnarzt-mller-psb.de" are the same host.
    """
    try:
        host = (urlparse(url if "//" in url else "//" + url).hostname or "").rstrip(".")
    except ValueError:
        return ""
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return host.lower()


def site_key(url):
    """Rate-limit key: the host without a leading "www.", so www.x.com and x.com share a limit."""
    host = host_of(url)
    return host[4:] if host.startswith("www.") else host


def host_matches(url, hosts):
    host = host_of(url)
    return any(host == h or host.endswith("." + h) for h in hosts)


def listing_host(url):
    """Return the listing/social host a URL points to (e.g. 'facebook.com'), or None."""
    host = host_of(url)
    if GOOGLE_HOST.search(host):
        return "google.com"
    for h in LISTING_HOSTS:
        if host == h or host.endswith("." + h):
            return h
    return None


# Social networks we list on lead cards (links only: these sites are never fetched).
SOCIAL_NETWORKS = (
    ("Facebook", ("facebook.com", "fb.com", "fb.me")),
    ("Instagram", ("instagram.com", "instagr.am")),
    ("X", ("twitter.com", "x.com")),
    ("LinkedIn", ("linkedin.com",)),
    ("TikTok", ("tiktok.com",)),
    ("YouTube", ("youtube.com",)),
)
# Paths that are share buttons, pixels, widgets or policy pages rather than someone's profile.
NOT_A_PROFILE = re.compile(
    r"^/(sharer|share|plugins|dialog|tr|intent|hashtag|home|login|signup|policies|privacy|legal|help|"
    r"watch|embed|search|results|about|terms)\b",
    re.I,
)


def social_profile(url):
    """Return (network, url) if `url` links to a social media profile or page, else None."""
    host = host_of(url)
    for network, hosts in SOCIAL_NETWORKS:
        if not any(host == h or host.endswith("." + h) for h in hosts):
            continue
        try:
            parts = urlparse(url)
        except ValueError:
            return None
        if network == "Facebook" and parts.path.startswith("/plugins"):  # page widget: the page is in ?href=
            href = parse_qs(parts.query).get("href", [""])[0]
            return social_profile(href) if href.startswith("http") else None
        if not parts.path.strip("/") or NOT_A_PROFILE.match(parts.path):
            return None
        return network, url.split("#")[0]
    return None
