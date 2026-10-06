"""Find clinics in a city using the OpenStreetMap Overpass API."""

import os
import re

import requests

DEFAULT_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]


def _regex_literal(text):
    """Escape text for an Overpass regex, matching the whole value."""
    return "^" + re.sub(r'([\\.^$|?*+()\[\]{}"])', r"\\\1", text.strip()) + "$"


def build_query(city, country, types, limit):
    country_re = _regex_literal(country)
    city_re = _regex_literal(city)
    types_re = "^(" + "|".join(re.escape(t) for t in types) + ")$"
    return f"""
[out:json][timeout:90];
area["boundary"="administrative"]["admin_level"="2"][~"^(name|name:en|int_name)$"~"{country_re}",i]->.country;
rel["boundary"="administrative"][~"^(name|name:en|int_name)$"~"{city_re}",i](area.country);
map_to_area->.city;
nwr["amenity"~"{types_re}"]["name"](area.city);
out tags center {int(limit)};
""".strip()


def _address(tags):
    if tags.get("addr:full"):
        return tags["addr:full"]
    street = " ".join(p for p in (tags.get("addr:housenumber"), tags.get("addr:street")) if p)
    town = " ".join(p for p in (tags.get("addr:postcode"), tags.get("addr:city")) if p)
    return ", ".join(p for p in (street, town) if p)


def parse_elements(data):
    clinics = []
    seen = set()
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name", "").strip()
        if not name:
            continue
        website = (tags.get("website") or tags.get("contact:website") or tags.get("url") or "").strip()
        key = (name.lower(), website.lower())
        if key in seen:  # same clinic mapped as both node and building
            continue
        seen.add(key)
        clinics.append({
            "name": name,
            "type": tags.get("amenity", ""),
            "phone": (tags.get("phone") or tags.get("contact:phone") or "").strip(),
            "address": _address(tags),
            "website": website,
            "opening_hours": tags.get("opening_hours", ""),
            "osm_id": f"{el.get('type')}/{el.get('id')}",
        })
    return clinics


def find_clinics(http, city, country, types, limit):
    """Return a list of clinic dicts. Tries each Overpass endpoint in turn."""
    query = build_query(city, country, types, limit)
    custom = os.getenv("OVERPASS_URL", "").strip()
    endpoints = [custom] if custom else DEFAULT_ENDPOINTS
    errors = []
    for url in endpoints:
        try:
            resp = http.post(url, data={"data": query}, timeout=120)
            resp.raise_for_status()
            return parse_elements(resp.json())
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("All Overpass endpoints failed:\n  " + "\n  ".join(errors))
