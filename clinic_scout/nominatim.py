"""City lookup (and a fallback clinic search) via the free OSM Nominatim API.

Usage policy: https://operations.osmfoundation.org/policies/nominatim/
We send a handful of requests per run, at most one per second, and cache them.
"""

from clinic_scout import cache

SEARCH_URL = "https://nominatim.openstreetmap.org/search"
MAX_PAGES_PER_TYPE = 3  # Nominatim returns at most 40 results per page
# Common 2-letter inputs that aren't the ISO code Nominatim expects.
COUNTRY_CODE_ALIASES = {"uk": "gb", "el": "gr"}


def _get(http, params, refresh=False):
    params = {"format": "jsonv2", **params}
    key = repr(sorted(params.items()))
    cached = None if refresh else cache.load("nominatim", key)
    if cached is not None:
        return cached
    resp = http.get(SEARCH_URL, params=params, timeout=(10, 30), attempts=3)
    resp.raise_for_status()
    data = resp.json()
    if data:  # don't let an empty (possibly transient) answer stick for 24 hours
        cache.save("nominatim", key, data)
    return data


def find_city(http, city, country, region=None, refresh=False):
    """Return the best match for the city, or None.

    The result has: name, display_name, country_code, osm_type, osm_id, lat, lon,
    bbox (south, north, west, east) and `others` (display names of other matches).
    """
    params = {"city": city, "limit": 5, "addressdetails": 1}
    if region:
        params["state"] = region
    code = country.strip().lower()
    if len(code) == 2:
        params["countrycodes"] = COUNTRY_CODE_ALIASES.get(code, code)
    else:
        params["country"] = country
    results = _get(http, params, refresh)
    if not results:  # structured search is strict; retry as free text
        free = {"q": ", ".join(p for p in (city, region, country) if p), "limit": 5, "addressdetails": 1}
        if "countrycodes" in params:
            free = {"q": ", ".join(p for p in (city, region) if p), "limit": 5, "addressdetails": 1,
                    "countrycodes": params["countrycodes"]}
        results = _get(http, free, refresh)
    if not results:
        return None
    best = results[0]
    south, north, west, east = (float(x) for x in best["boundingbox"])
    return {
        "name": best.get("name") or city,
        "display_name": best["display_name"],
        "country_code": (best.get("address") or {}).get("country_code", ""),
        "osm_type": best["osm_type"],
        "osm_id": int(best["osm_id"]),
        "lat": float(best["lat"]),
        "lon": float(best["lon"]),
        "bbox": (south, north, west, east),
        "others": [r["display_name"] for r in results[1:]],
    }


def _as_tags(result):
    """Turn a Nominatim result into OSM-style tags so the Overpass parser can read it."""
    tags = dict(result.get("extratags") or {})
    tags[result["category"]] = result["type"]
    tags["name"] = result.get("name", "")
    addr = result.get("address") or {}
    tags.setdefault("addr:housenumber", addr.get("house_number", ""))
    tags.setdefault("addr:street", addr.get("road", ""))
    tags.setdefault("addr:postcode", addr.get("postcode", ""))
    tags.setdefault("addr:city", addr.get("city") or addr.get("town") or addr.get("village") or "")
    return tags


def search_clinics(http, city_info, types, limit, refresh=False):
    """Fallback when every Overpass server is down: search each type inside the city's box.

    Returns raw elements in Overpass JSON shape.
    """
    south, north, west, east = city_info["bbox"]
    elements = []
    for amenity in types:
        seen_ids = []
        for _ in range(MAX_PAGES_PER_TYPE):
            params = {
                "q": amenity, "limit": 40, "extratags": 1, "addressdetails": 1,
                "viewbox": f"{west},{north},{east},{south}", "bounded": 1,
            }
            if seen_ids:
                params["exclude_place_ids"] = ",".join(seen_ids)
            page = _get(http, params, refresh)
            for r in page:
                seen_ids.append(str(r["place_id"]))
                if r.get("type") == amenity and r.get("name"):
                    elements.append({
                        "type": r["osm_type"], "id": r["osm_id"],
                        "center": {"lat": float(r["lat"]), "lon": float(r["lon"])},
                        "tags": _as_tags(r),
                    })
            if len(page) < 40 or len(elements) >= limit:
                break
        if len(elements) >= limit:
            break
    return {"elements": elements[:limit]}
