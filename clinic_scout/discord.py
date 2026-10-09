"""Post leads to a Discord channel through a webhook: one card (embed) per lead.

Set DISCORD_WEBHOOK_URL in .env (or as a GitHub Actions secret). Create one in Discord:
channel settings -> Integrations -> Webhooks -> New Webhook -> Copy Webhook URL.
The Google, Maps and WhatsApp entries are plain links for you to tap; nothing here fetches them.
"""

import re
from urllib.parse import quote, quote_plus

import requests

from clinic_scout.hosts import listing_host

WEBHOOK_URL = re.compile(r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+/?$")
# Discord limits: https://discord.com/developers/docs/resources/message#embed-object-embed-limits
TITLE_MAX, DESCRIPTION_MAX, FIELD_MAX, EMBED_TOTAL_MAX = 256, 4096, 1024, 6000
SHARE_TEXT_MAX = 400
LINK_MAX = 1000  # a single link must fit in one 1024-character field
FOOTER = "clinic-scout · Clinic data © OpenStreetMap contributors (ODbL)"
NO_MENTIONS = {"parse": []}  # a clinic called "@everyone" must not ping the channel


def is_webhook_url(url):
    return bool(WEBHOOK_URL.match(url or ""))


def _clip(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _escape(text):
    """Stop names and notes from being read as Discord formatting (bold, strikethrough, spoilers...)."""
    return re.sub(r"([\\*_~`|])", r"\\\1", text or "")


def _links(pairs, limit=FIELD_MAX):
    """'[label](url) · [label](url)', keeping only whole links that fit in `limit`."""
    out = ""
    for label, url in pairs:
        if not url:
            continue
        item = f"[{label}]({url})"
        joined = f"{out} · {item}" if out else item
        if len(joined) > limit:
            break
        out = joined
    return out


def _lookup_text(lead, city):
    return " ".join(p for p in (_clip(lead["name"], 100), _clip(lead.get("address") or city, 150)) if p)


def google_search_url(lead, city):
    return "https://www.google.com/search?q=" + quote_plus(_lookup_text(lead, city))


def google_maps_url(lead, city):
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(_lookup_text(lead, city))


def whatsapp_share_url(lead, city):
    lines = [
        f"Lead: {lead['name']} (score {lead['score']})",
        lead.get("note", ""),
        f"Phone: {lead['phone']}" if lead.get("phone") else "",
        f"Email: {', '.join(lead['emails'])}" if lead.get("emails") else "",
        f"Address: {lead['address']}" if lead.get("address") else "",
        f"Website: {lead['website']}" if lead.get("website") else "",
        f"Maps: {google_maps_url(lead, city)}",
    ]
    details, maps = "\n".join(line for line in lines[:-1] if line), lines[-1]
    budget = SHARE_TEXT_MAX
    while True:  # shorten the details (never the Maps link) until the whole link fits in a card field
        text = _clip(details, max(budget - len(maps) - 1, 20)) + "\n" + maps
        url = "https://wa.me/?text=" + quote(text, safe="")
        if len(url) <= LINK_MAX or budget <= len(maps) + 21:
            return url
        budget -= 50


def _color(score):
    if score >= 10:
        return 0xE74C3C  # red: no website or a broken one
    if score >= 5:
        return 0xE67E22  # orange
    if score >= 3:
        return 0xF1C40F  # yellow
    return 0x95A5A6  # grey


def build_card(rank, lead, city):
    """One Discord embed for one lead, within Discord's size limits."""
    socials = _links(sorted(lead.get("socials", {}).items()))
    listed = _links([("OpenStreetMap", lead.get("osm_url")),
                     (_listing_label(lead.get("listing_url")), lead.get("listing_url"))])
    lookup = _links([("Search on Google", google_search_url(lead, city)),
                     ("Open in Google Maps", google_maps_url(lead, city))])
    share = _links([("Share on WhatsApp", whatsapp_share_url(lead, city))])
    website = lead.get("website") or ""
    fields = [
        ("Score", str(lead["score"]), True),
        ("Phone", _escape(lead.get("phone")) or "Not listed", True),
        ("Email", _escape(", ".join(lead.get("emails") or [])) or "Not found", True),
        ("Address", _escape(lead.get("address")) or "Not listed", False),
        ("Website", website if website.startswith("http") else _escape(website) or "None found", False),
        ("What's wrong", _escape(lead.get("issues")) or "Nothing found", False),
        ("Social media", socials or "None found", False),
        ("Where they're listed", listed or "Not found", False),
        ("Look them up", lookup, False),
        ("Share", share, False),
    ]
    embed = {
        "title": _clip(f"#{rank} · {lead['name']}", TITLE_MAX),
        "description": _clip(_escape(lead.get("note")), DESCRIPTION_MAX),
        "color": _color(lead["score"]),
        "fields": [{"name": n, "value": _clip(v, FIELD_MAX), "inline": i} for n, v, i in fields],
        "footer": {"text": FOOTER},
    }
    if website.startswith("http"):
        embed["url"] = website
    _fit(embed)
    return embed


def _listing_label(url):
    return f"{listing_host(url)} page" if url else ""


def _size(embed):
    return (len(embed["title"]) + len(embed["description"]) + len(embed["footer"]["text"])
            + sum(len(f["name"]) + len(f["value"]) for f in embed["fields"]))


def _fit(embed):
    """Trim the longest free-text parts until the card is under Discord's 6000-character total."""
    while _size(embed) > EMBED_TOTAL_MAX:
        over = _size(embed) - EMBED_TOTAL_MAX
        issues = next(f for f in embed["fields"] if f["name"] == "What's wrong")
        if len(issues["value"]) > 100:
            issues["value"] = _clip(issues["value"], max(100, len(issues["value"]) - over))
        else:
            embed["description"] = _clip(embed["description"], max(50, len(embed["description"]) - over))
            if len(embed["description"]) <= 50:
                break


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
