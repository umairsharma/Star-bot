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
SHARE_LINK_MAX = 3500  # the WhatsApp link goes in the card description (4096 max)
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


def _listing_label(url):
    return f"{listing_host(url)} page" if url else ""


EMPTY_LINKS = {"Social media": "None found", "Where they're listed": "Not found"}
FREE_TEXT_FIELDS = {"Address", "What's wrong"}  # shortened first when a WhatsApp link gets too long


def card_sections(lead, city):
    """The lead's details in card order, shared by the Discord card and the WhatsApp message
    so the two always match.

    Returns (fields, link_groups): fields are (label, text); link groups are (label, [(name, url), ...]).
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
    links = [
        ("Social media", sorted((lead.get("socials") or {}).items())),
        ("Where they're listed", [(name, url) for name, url in
                                  (("OpenStreetMap", lead.get("osm_url")), (_listing_label(listing), listing)) if url]),
        ("Look them up", [("Search on Google", google_search_url(lead, city)),
                          ("Open in Google Maps", google_maps_url(lead, city))]),
    ]
    return fields, links


def whatsapp_text(rank, lead, city, clip=None):
    """Everything on the card as a WhatsApp message (*bold* labels, one link per line).

    `clip` shortens the free-text parts (name, note, address, issues) when the link would be too long.
    """
    fields, links = card_sections(lead, city)
    cut = (lambda text: _clip(text, clip)) if clip else (lambda text: text)
    lines = [f"*#{rank} · {cut(lead['name'])}*", cut(lead.get("note") or ""), ""]
    for label, text in fields:
        lines.append(f"*{label}:* {cut(text) if label in FREE_TEXT_FIELDS else text}")
    for label, pairs in links:
        if pairs:
            lines.append(f"*{label}:*")
            lines += [f"• {name}: {url}" for name, url in pairs]
        else:
            lines.append(f"*{label}:* {EMPTY_LINKS.get(label, '')}")
    return "\n".join(line for i, line in enumerate(lines) if line or i == 2).strip()


def whatsapp_share_url(rank, lead, city):
    """wa.me link pre-filled with the whole card. Long free text is shortened, links are kept."""
    for clip in (None, 300, 120, 50):
        url = "https://wa.me/?text=" + quote(whatsapp_text(rank, lead, city, clip), safe=":/,@")
        if len(url) <= SHARE_LINK_MAX:
            break
    return url


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
    fields, links = card_sections(lead, city)
    embed_fields = []
    for label, text in fields:
        value = text if label == "Website" and text.startswith("http") else _escape(text)
        embed_fields.append({"name": label, "value": _clip(value, FIELD_MAX),
                             "inline": label in ("Score", "Phone", "Email")})
    for label, pairs in links:
        embed_fields.append({"name": label, "value": _links(pairs) or EMPTY_LINKS.get(label, "-"), "inline": False})
    website = lead.get("website") or ""
    embed = {
        "title": _clip(f"#{rank} · {lead['name']}", TITLE_MAX),
        "description": "",
        "color": _color(lead["score"]),
        "fields": embed_fields,
        "footer": {"text": FOOTER},
    }
    if website.startswith("http"):
        embed["url"] = website
    share = f"[📲 Share on WhatsApp (full lead details)]({whatsapp_share_url(rank, lead, city)})"
    _fit(embed, _escape(lead.get("note")), share)
    return embed


def _size(embed):
    return (len(embed["title"]) + len(embed["description"]) + len(embed["footer"]["text"])
            + sum(len(f["name"]) + len(f["value"]) for f in embed["fields"]))


def _fit(embed, note, share):
    """Set the description (note, then the WhatsApp link) and keep the whole card under
    Discord's 6000-character total. The link is never cut; the issues list and note are."""
    tail = "\n\n" + share
    issues = next(f for f in embed["fields"] if f["name"] == "What's wrong")
    over = _size(embed) + len(tail) + 100 - EMBED_TOTAL_MAX  # leave room for at least some of the note
    if over > 0 and len(issues["value"]) > 100:
        issues["value"] = _clip(issues["value"], max(100, len(issues["value"]) - over))
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
