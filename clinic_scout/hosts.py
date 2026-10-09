"""Host helpers shared by the fetcher, robots.txt handling and the website finders.

LISTING_HOSTS are sites that are *about* a clinic (social pages, directories,
review sites, Google) rather than the clinic's own website. They are never
fetched, not even through a redirect or a robots.txt request.
"""

import re
from urllib.parse import parse_qs, urlparse, urlsplit

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
# First path segments that are share buttons, pixels, widgets, posts or site pages, not an account.
NOT_A_PROFILE = {
    "sharer", "sharer.php", "share", "share.php", "sharing", "sharearticle", "cws", "plugins", "dialog", "tr",
    "intent", "hashtag", "home", "home.php", "login", "login.php", "signup", "policies", "privacy", "legal",
    "help", "watch", "embed", "search", "results", "about", "terms", "explore", "accounts", "p", "reel", "reels",
    "tv", "stories", "l.php", "photo", "photo.php", "photos", "video", "videos", "events", "groups", "i", "status",
    "feed", "messages", "notifications", "settings", "shorts", "playlist", "post", "posts", "pulse", "jobs",
    "tag", "discover", "music",
}
# Accounts whose address spans several segments: facebook.com/pages/Name/123, linkedin.com/company/x, ...
PROFILE_PREFIXES = {
    "Facebook": ("pages", "people"),
    "LinkedIn": ("company", "in", "school", "showcase"),
    "YouTube": ("channel", "c", "user"),
}


def social_network(url):
    """Name of the social network a URL belongs to ("Facebook", ...), or None."""
    host = host_of(url)
    return next((name for name, hosts in SOCIAL_NETWORKS
                 if any(host == h or host.endswith("." + h) for h in hosts)), None)


def social_profile(url):
    """Return (network, profile_url) if `url` points at a social media account, else None.

    Share buttons, pixels, posts and site pages are rejected; links to a post or video
    are trimmed to the account itself (x.com/smile/status/1 -> x.com/smile).
    """
    network = social_network(url)
    if not network:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if segments and re.fullmatch(r"v\d+(\.\d+)?", segments[0]):  # versioned paths: /v2.12/plugins/...
        segments = segments[1:]
    if not segments:
        return None
    first, query = segments[0].lower(), parse_qs(parts.query)
    if network == "Facebook" and first == "plugins":  # page widget: the page is in ?href=
        href = query.get("href", [""])[0]
        return social_profile(href) if href.startswith("http") else None
    if network == "Facebook" and first == "profile.php":
        return (network, f"https://www.facebook.com/profile.php?id={query['id'][0]}") if query.get("id") else None
    if first in NOT_A_PROFILE or "u" in query or "url" in query:
        return None
    base = f"https://{parts.netloc}"
    if first in PROFILE_PREFIXES.get(network, ()):
        keep = 3 if network == "Facebook" else 2
        return (network, f"{base}/{'/'.join(segments[:keep])}") if len(segments) >= 2 else None
    if network == "LinkedIn" or (network == "YouTube" and not first.startswith("@")):
        return None  # those accounts always use one of the prefixes (or @handle) above
    return network, f"{base}/{segments[0]}"


EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}\b")
# Things that look like emails but aren't contact addresses (image names, tracking, placeholders).
JUNK_EMAIL = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|css|js)$|@(example\.|sentry|wixpress\.com|domain\.com|email\.com|yourdomain)",
    re.I,
)
MAX_EMAILS = 3


def clean_emails(candidates):
    """Valid, de-duplicated (case-insensitive) emails, without "mailto:" prefixes; at most MAX_EMAILS."""
    emails, seen = [], set()
    for raw in candidates:
        for email in re.split(r"[;,\s]+", raw or ""):
            email = re.sub(r"^mailto:", "", email.strip(), flags=re.I)
            if EMAIL.fullmatch(email) and not JUNK_EMAIL.search(email) and email.lower() not in seen:
                seen.add(email.lower())
                emails.append(email)
    return emails[:MAX_EMAILS]
