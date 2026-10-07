"""Find clinics in a city using the free OpenStreetMap Overpass API."""

import math
import os
import re

import requests

from clinic_scout import cache
from clinic_scout.hosts import listing_host

DEFAULT_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
# OSM's newer `healthcare=*` tag uses slightly different values than `amenity=*`.
HEALTHCARE_EQUIVALENT = {"doctors": "doctor"}
DUPLICATE_DISTANCE_M = 150
# Public and hospital units: not marketing prospects, skipped unless --include-public.
PUBLIC_NAME = re.compile(r"\b(nhs|hospital|infirmary)\b", re.I)
PUBLIC_OPERATOR = re.compile(r"\b(nhs|university)\b", re.I)
# Extra signals that only mean "public" where healthcare is mostly public (the UK's NHS).
# Elsewhere, e.g. in the US, walk-in, emergency and oncology clinics are private businesses.
NHS_COUNTRIES = {"gb"}
NHS_NAME = re.compile(r"\bwalk[- ]in\b", re.I)
NHS_OPERATOR = re.compile(r"\btrust\b", re.I)
HOSPITAL_SPECIALITIES = {"cystic_fibrosis", "oncology", "radiotherapy", "dialysis", "emergency", "intensive_care"}


def build_query(city_info, types, radius_m):
    """Search the whole city: inside its boundary, or around its centre if it has none."""
    if city_info["osm_type"] == "relation":
        scope_setup = f"area({3600000000 + city_info['osm_id']})->.city;"
        scope = "(area.city)"
    else:
        scope_setup = ""
        scope = f"(around:{int(radius_m)},{city_info['lat']},{city_info['lon']})"
    amenity_re = "^(" + "|".join(types) + ")$"
    healthcare_re = "^(" + "|".join(HEALTHCARE_EQUIVALENT.get(t, t) for t in types) + ")$"
    return f"""
[out:json][timeout:120];
{scope_setup}
(
  nwr["amenity"~"{amenity_re}"]["name"]{scope};
  nwr["healthcare"~"{healthcare_re}"]["name"]{scope};
);
out tags center;
""".strip()


def run_query(http, query, refresh=False):
    """POST the query to each Overpass server in turn; return the JSON response."""
    if not refresh:
        cached = cache.load("overpass", query)
        if cached is not None:
            return cached
    custom = os.getenv("OVERPASS_URL", "").strip()
    endpoints = [custom] if custom else DEFAULT_ENDPOINTS
    errors = []
    for url in endpoints:
        try:
            resp = http.post(url, data={"data": query}, timeout=(10, 150), attempts=2)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{url}: {exc}")
            continue
        if "remark" in data and not data.get("elements"):  # server-side timeout or error
            errors.append(f"{url}: {data['remark']}")
            continue
        cache.save("overpass", query, data)
        return data
    raise RuntimeError("All Overpass servers failed:\n  " + "\n  ".join(errors))


def _address(tags):
    if tags.get("addr:full"):
        return tags["addr:full"]
    street = " ".join(p for p in (tags.get("addr:housenumber"), tags.get("addr:street")) if p)
    town = " ".join(p for p in (tags.get("addr:postcode"), tags.get("addr:city")) if p)
    return ", ".join(p for p in (street, town) if p)


def is_public_unit(tags, country_code=""):
    names = " ".join(tags.get(k, "") for k in ("name", "alt_name", "official_name", "short_name"))
    operator = tags.get("operator", "")
    if (PUBLIC_NAME.search(names) or PUBLIC_OPERATOR.search(operator)
            or tags.get("healthcare") == "hospital"
            or tags.get("operator:type") in ("public", "government")):
        return True
    if country_code.lower() not in NHS_COUNTRIES:
        return False
    specialities = set(re.split(r"\s*;\s*", tags.get("healthcare:speciality", "")))
    return bool(
        NHS_NAME.search(names)
        or NHS_OPERATOR.search(operator)
        or tags.get("building") == "hospital"
        or specialities & HOSPITAL_SPECIALITIES
    )


def _website(tags):
    """The clinic's website tag. OSM allows several values ("a;b"): prefer one that isn't a listing page."""
    raw = tags.get("website") or tags.get("contact:website") or tags.get("url") or ""
    values = [v.strip() for v in raw.split(";") if v.strip()]
    return next((v for v in values if not listing_host(v)), values[0] if values else "")


def _distance_m(a, b):
    if None in (a["lat"], b["lat"]):
        return math.inf
    dlat = math.radians(b["lat"] - a["lat"])
    dlon = math.radians(b["lon"] - a["lon"]) * math.cos(math.radians(a["lat"]))
    return 6371000 * math.hypot(dlat, dlon)


def parse_elements(data, country_code=""):
    """Turn Overpass elements into clinic dicts, skipping unnamed ones.

    The same clinic is often mapped twice (a point plus its building), so two
    entries with the same name within DUPLICATE_DISTANCE_M are merged.
    """
    clinics = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name", "").strip()
        if not name:
            continue
        center = el.get("center") or {"lat": el.get("lat"), "lon": el.get("lon")}
        clinic = {
            "name": name,
            "type": tags.get("amenity") or tags.get("healthcare", ""),
            "phone": (tags.get("phone") or tags.get("contact:phone") or "").strip(),
            "address": _address(tags),
            "website": _website(tags),
            "opening_hours": tags.get("opening_hours", ""),
            "postcode": tags.get("addr:postcode", ""),
            "street": tags.get("addr:street", ""),
            "public": is_public_unit(tags, country_code),
            "osm_url": f"https://www.openstreetmap.org/{el.get('type')}/{el.get('id')}",
            "lat": center.get("lat"),
            "lon": center.get("lon"),
        }
        duplicate = next(
            (c for c in clinics
             if c["name"].lower() == name.lower() and _distance_m(c, clinic) <= DUPLICATE_DISTANCE_M),
            None,
        )
        if duplicate:
            for field, value in clinic.items():
                if value and not duplicate[field]:
                    duplicate[field] = value
        else:
            clinics.append(clinic)
    return clinics
