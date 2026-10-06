"""Turn website-check issues into a score and a one-sentence note."""

import re

# Points per weakness. Higher score = more likely to need marketing help.
WEIGHTS = {
    "no_website": 10,  # confirmed: not on the map, at guessed domains, or in Brave search
    "no_website_unverified": 3,  # not on the map or at guessed domains, but not searched
    "website_dead": 10,
    "no_https": 2,
    "no_viewport": 2,
    "slow": 1,
    "no_booking": 2,
    "old_copyright": 1,
    "no_ads": 1,
    "no_social": 2,
}

# How each issue reads in the `issues` column ({} is the issue's detail).
LABELS = {
    "no_website": "no own website ({})",
    "no_website_unverified": "no website found ({}), not verified",
    "website_dead": "website down ({})",
    "no_https": "no HTTPS",
    "no_viewport": "not mobile-friendly (no viewport tag)",
    "slow": "slow homepage ({})",
    "no_booking": "no booking form, WhatsApp or tel: link",
    "old_copyright": "copyright year {}",
    "no_ads": "no Meta Pixel or Google Ads tags",
    "no_social": "no Facebook or Instagram links",
}

# How each issue reads inside the note sentence ("Site ..., ... and ...").
NOTE_PHRASES = {
    "no_https": "has no HTTPS",
    "no_viewport": "isn't mobile-friendly",
    "slow": "loads slowly",
    "no_booking": "has no way to book or tap-to-call",
    "old_copyright": "looks outdated",
    "no_ads": "shows no sign of running ads",
    "no_social": "has no social media links",
}
NOTE_MAX_PHRASES = 3


def score(issues):
    return sum(WEIGHTS[code] for code, _ in issues)


def issues_text(issues, info=()):
    parts = []
    for code, detail in issues:
        label = LABELS[code]
        if "{}" in label:
            label = label.format(detail) if detail else label.replace(" ({})", "").replace(" {}", "")
        elif detail:
            label += f" ({detail})"
        parts.append(label)
    return "; ".join(parts + list(info))


def note(issues, info=()):
    codes = {code: detail for code, detail in issues}
    if "no_website" in codes:
        listing = re.search(r"\ba (\S+\.\S+) page", codes["no_website"])
        if listing:
            return f"Their only web presence is a {listing.group(1)} page, so they're hard to find and book online."
        return "No website found anywhere, so new patients can't find or book them online."
    if "no_website_unverified" in codes:
        listing = re.search(r"\ba (\S+\.\S+) page", codes["no_website_unverified"])
        lead = f"Map links only to a {listing.group(1)} page" if listing else "No website on the map"
        return f"{lead} and none found at likely domains; worth a quick manual check."
    if "website_dead" in codes:
        return f"Their website is broken ({codes['website_dead']}), so online visitors hit a dead end."
    ranked = sorted((c for c in codes if c in NOTE_PHRASES), key=lambda c: -WEIGHTS[c])
    if not ranked:
        not_checked = [i for i in info if i.startswith("not checked")]
        if not_checked:
            return f"Website {not_checked[0]}."
        return "No obvious website problems found."
    shown = [NOTE_PHRASES[c] for c in ranked[:NOTE_MAX_PHRASES]]
    sentence = "Site " + (shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1])
    extra = len(ranked) - len(shown)
    return sentence + (f" (+{extra} more issue{'s' if extra > 1 else ''})." if extra else ".")
