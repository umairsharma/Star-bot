"""Post leads to a Discord channel through a webhook: one card (embed) per lead.

Set DISCORD_WEBHOOK_URL in .env (or as a GitHub Actions secret). Create one in Discord:
channel settings -> Integrations -> Webhooks -> New Webhook -> Copy Webhook URL.
The Google, Maps and WhatsApp entries are plain links for you to tap; nothing here fetches them.
"""

import re
from urllib.parse import quote, quote_plus, urlsplit

import requests

from clinic_scout.hosts import listing_host

WEBHOOK_URL = re.compile(r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+/?$")
# Discord limits: https://discord.com/developers/docs/resources/message#embed-object-embed-limits
TITLE_MAX, DESCRIPTION_MAX, FIELD_MAX, EMBED_TOTAL_MAX = 256, 4096, 1024, 6000
SHARE_LINK_MAX = 3500  # the WhatsApp link goes in the card description (4096 max)
LOOKUP_URL_MAX = 450  # Google and Maps links share one 1024-character field, so each gets under half
SHARE_LABEL = "📲 Share on WhatsApp (full lead details)"
FOOTER = "clinic-scout · Clinic data © OpenStreetMap contributors (ODbL)"
NO_MENTIONS = {"parse": []}  # a clinic called "@everyone" must not ping the channel


def is_webhook_url(url):
    return bool(WEBHOOK_URL.match(url or ""))


def _clip(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def _escape(text):
    """Stop names and notes from being read as Discord formatting (bold, strikethrough, spoilers...)."""
    return re.sub(r"([\\*_~`|])", r"\\\1", text or "")


def safe_url(url):
    """A link that's safe inside a Discord masked link: http(s), a real domain name, and no
    spaces or parentheses. Adds https:// when missing; returns "" if it can't be fixed."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        port = f":{parts.port}" if parts.port else ""
    except (ValueError, UnicodeError):
        return ""
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", host):
        return ""
    path = quote(parts.path, safe="/%~@:!$&'*+,;=")  # encodes spaces, ( ) and non-ASCII
    query = "?" + quote(parts.query, safe="/%~@:!$&'*+,;=?") if parts.query else ""
    return f"{parts.scheme.lower()}://{host}{port}{path}{query}"


def _links(pairs, limit=FIELD_MAX):
    """'[label](url) · [label](url)', keeping only valid links, each whole, within `limit`."""
    out = ""
    for label, url in pairs:
        url = safe_url(url)
        if not url:
            continue
        item = f"[{label}]({url})"
        joined = f"{out} · {item}" if out else item
        if len(joined) > limit:
            break
        out = joined
    return out


def _lookup_text(lead, city):
    """Name, address and (if the address doesn't already say) the town, so the search finds this clinic."""
    address = lead.get("address") or ""
    town = city.split(", ")[0] if city else ""
    place = ", ".join(city.split(", ")[:2]) if town and town.lower() not in address.lower() else ""
    return " ".join(p for p in (lead["name"], address, place) if p)


def _lookup_url(base, lead, city):
    text = _lookup_text(lead, city)
    url = base + quote_plus(text)
    while len(url) > LOOKUP_URL_MAX and len(text) > 10:  # non-Latin text grows ~6-9x when encoded
        text = text[: int(len(text) * 0.8)]
        url = base + quote_plus(text)
    return url


def google_search_url(lead, city):
    return _lookup_url("https://www.google.com/search?q=", lead, city)


def google_maps_url(lead, city):
    return _lookup_url("https://www.google.com/maps/search/?api=1&query=", lead, city)


def _listing_label(url):
    return f"{listing_host(url)} page" if url else ""


EMPTY_LINKS = {"Social media": "None found", "Where they're listed": "Not found"}
FREE_TEXT_FIELDS = {"Address", "What's wrong"}  # shortened first when a WhatsApp link gets too long


def card_sections(lead, city):
    """The lead's details in card order, shared by the Discord card and the WhatsApp message
    so the two always match.

    Returns (fields, link_groups): fields are (label, text); link groups are (label, [(name, url), ...])
    with only valid, cleaned-up URLs.
    """
    listing = lead.get("listing_url")
    fields = [
        ("Score", str(lead["score"])),
        ("Phone", lead.get("phone") or "Not listed"),
        ("Email", ", ".join(lead.get("emails") or []) or "Not found"),
        ("Address", lead.get("address") or "Not listed"),
        ("Website", lead.get("website") or "None found"),
        ("What's wrong", lead.get("issues") or "Nothing found"),
    ]
    groups = [
        ("Social media", sorted((lead.get("socials") or {}).items())),
        ("Where they're listed", [("OpenStreetMap", lead.get("osm_url")), (_listing_label(listing), listing)]),
        ("Look them up", [("Search on Google", google_search_url(lead, city)),
                          ("Open in Google Maps", google_maps_url(lead, city))]),
    ]
    links = [(label, [(name, safe_url(url)) for name, url in pairs if safe_url(url)]) for label, pairs in groups]
    return fields, links


def whatsapp_text(rank, lead, city, clip=None, lookup_links=True):
    """Everything on the card as a WhatsApp message (*bold* labels, one link per line).

    `clip` shortens the free text (name, note, address, issues); `lookup_links=False` leaves out
    the long Google/Maps links. Both are only used when the full message won't fit.
    """
    fields, links = card_sections(lead, city)
    cut = (lambda text: _clip(text, clip)) if clip else (lambda text: text)
    lines = [f"*#{rank} · {cut(lead['name'])}*", cut(lead.get("note") or ""), ""]
    for label, text in fields:
        lines.append(f"*{label}:* {cut(text) if label in FREE_TEXT_FIELDS else text}")
    for label, pairs in links:
        if label == "Look them up" and not lookup_links:
            continue
        if pairs:
            lines.append(f"*{label}:*")
            lines += [f"• {name}: {url}" for name, url in pairs]
        else:
            lines.append(f"*{label}:* {EMPTY_LINKS.get(label, '')}")
    return "\n".join(line for i, line in enumerate(lines) if line or i == 2).strip()


def _wa(text):
    return "https://wa.me/?text=" + quote(text, safe=":/,@")


def whatsapp_share_url(rank, lead, city, max_len=SHARE_LINK_MAX):
    """wa.me link pre-filled with the whole card, never longer than `max_len`.

    If the full message doesn't fit, the free text is shortened, then the Google/Maps links are
    dropped, and as a last resort only the name, phone and website are sent.
    """
    for clip, lookup_links in ((None, True), (300, True), (120, True), (50, True), (50, False)):
        url = _wa(whatsapp_text(rank, lead, city, clip, lookup_links))
        if len(url) <= max_len:
            return url
    for keep in (60, 30, 10):
        fallback = "\n".join(p for p in (f"*{_clip(lead['name'], keep)}*", lead.get("phone"),
                                          _clip(safe_url(lead.get("website")), 120)) if p)
        url = _wa(fallback)
        if len(url) <= max_len:
            return url
    return _wa(_clip(lead["name"], 5))


def _color(score):
    if score >= 10:
        return 0xE74C3C  # red: no website or a broken one
    if score >= 5:
        return 0xE67E22  # orange
    if score >= 3:
        return 0xF1C40F  # yellow
    return 0x95A5A6  # grey


def build_card(rank, lead, city):
    """One Discord embed for one lead, always within Discord's size limits."""
    fields, links = card_sections(lead, city)
    website = safe_url(lead.get("website")) if lead.get("website") else ""
    embed_fields = []
    for label, text in fields:
        value = website if label == "Website" and website else _escape(text)
        embed_fields.append({"name": label, "value": _clip(value, FIELD_MAX) or "-",
                             "inline": label in ("Score", "Phone", "Email")})
    for label, pairs in links:
        embed_fields.append({"name": label, "value": _links(pairs) or EMPTY_LINKS.get(label, "-"), "inline": False})
    embed = {
        "title": _clip(f"#{rank} · {lead['name']}", TITLE_MAX),
        "description": "",
        "color": _color(lead["score"]),
        "fields": embed_fields,
        "footer": {"text": FOOTER},
    }
    if website:
        embed["url"] = website
    _fit(embed, _escape(lead.get("note")), rank, lead, city)
    return embed


def _size(embed):
    return (len(embed["title"]) + len(embed["description"]) + len(embed["footer"]["text"])
            + sum(len(f["name"]) + len(f["value"]) for f in embed["fields"]))


def _fit(embed, note, rank, lead, city):
    """Fill the description (note, then the WhatsApp link) so the card stays within Discord's
    4096 description and 6000 total limits. The link is sized to the room left, never cut."""
    issues = next(f for f in embed["fields"] if f["name"] == "What's wrong")
    room = EMBED_TOTAL_MAX - _size(embed) - 100  # keep some of the note
    if room < 1500 and len(issues["value"]) > 100:  # make space for the share link
        issues["value"] = _clip(issues["value"], max(100, len(issues["value"]) - (1500 - room)))
    wrapper = len(f"\n\n[{SHARE_LABEL}]()")
    link_room = min(SHARE_LINK_MAX, DESCRIPTION_MAX - wrapper, EMBED_TOTAL_MAX - _size(embed) - wrapper)
    tail = f"\n\n[{SHARE_LABEL}]({whatsapp_share_url(rank, lead, city, max(link_room, 60))})"
    budget = min(DESCRIPTION_MAX, EMBED_TOTAL_MAX - _size(embed)) - len(tail)
    embed["description"] = (_clip(note, budget) if budget > 0 else "") + tail


def post_leads(http, webhook_url, leads, city, checked):
    """Post a header message, then one card per lead. Returns the number of cards posted.

    The webhook URL is a secret, so it never appears in printed messages.
    """
    def send(payload):
        resp = http.post(webhook_url, params={"wait": "true"}, timeout=(10, 20), attempts=3,
                         json={"username": "clinic-scout", "allowed_mentions": NO_MENTIONS, **payload})
        return resp.status_code, resp.text[:200]

    try:
        status, body = send({"content": f"**clinic-scout**: top {len(leads)} leads in {_escape(city)} "
                                        f"({checked} clinics checked)"})
    except requests.RequestException as exc:
        print(f"Discord: couldn't reach the webhook ({type(exc).__name__}); nothing posted.")
        return 0
    if status in (401, 403, 404):
        print(f"Discord: the webhook was rejected (HTTP {status}). Check DISCORD_WEBHOOK_URL; nothing posted.")
        return 0
    posted = 0
    for rank, lead in enumerate(leads, 1):
        try:
            status, body = send({"embeds": [build_card(rank, lead, city)]})
        except requests.RequestException as exc:
            print(f"Discord: card #{rank} failed ({type(exc).__name__}).")
            continue
        if status in (200, 204):
            posted += 1
        else:
            print(f"Discord: card #{rank} was refused (HTTP {status}): {body}")
    return posted
