"""Find clinics in a city using the free OpenStreetMap Overpass API."""

import math
import os

import requests

from clinic_scout import cache

DEFAULT_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
# OSM's newer `healthcare=*` tag uses slightly different values than `amenity=*`.
HEALTHCARE_EQUIVALENT = {"doctors": "doctor"}
DUPLICATE_DISTANCE_M = 150


def build_query(city_info, types, limit, radius_m):
    """Search inside the city's boundary, or around its centre if it has none."""
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
out tags center {int(limit)};
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


def _distance_m(a, b):
    if None in (a["lat"], b["lat"]):
        return math.inf
    dlat = math.radians(b["lat"] - a["lat"])
    dlon = math.radians(b["lon"] - a["lon"]) * math.cos(math.radians(a["lat"]))
    return 6371000 * math.hypot(dlat, dlon)


def parse_elements(data):
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
            "website": (tags.get("website") or tags.get("contact:website") or tags.get("url") or "").strip(),
            "opening_hours": tags.get("opening_hours", ""),
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
