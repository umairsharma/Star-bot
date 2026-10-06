"""Command-line entry point: find clinics, check their websites, score, export."""

import argparse
import re
import sys

import requests
from dotenv import load_dotenv

from clinic_scout import nominatim, output, overpass, scoring
from clinic_scout.brave import BraveLookup
from clinic_scout.http_client import PoliteSession, RobotsCache
from clinic_scout.website_checks import check_website, listing_host


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="clinic-scout",
        description="Find clinics in a city that likely need marketing help, using free data only.",
    )
    p.add_argument("--city", required=True)
    p.add_argument("--country", required=True, help="country name or 2-letter code (e.g. GB, US)")
    p.add_argument("--region", help="state/county, to pick the right city when names repeat")
    p.add_argument("--types", default="clinic,doctors,dentist",
                   help="comma-separated OSM amenity values (default: %(default)s)")
    p.add_argument("--limit", type=int, default=100, help="max clinics to check (default: %(default)s)")
    p.add_argument("--top", type=int, default=15, help="rows to keep in the CSV (default: %(default)s)")
    p.add_argument("--radius-km", type=float, default=5.0,
                   help="search radius if the city has no boundary on the map (default: %(default)s)")
    p.add_argument("--output", default="results.csv", help="CSV path (default: %(default)s)")
    p.add_argument("--refresh", action="store_true", help="ignore cached OpenStreetMap results")
    args = p.parse_args(argv)
    args.types = [t.strip().lower() for t in args.types.split(",") if t.strip()]
    if not args.types or not all(re.fullmatch(r"[a-z_]+", t) for t in args.types):
        p.error("--types must be comma-separated words like clinic,doctors,dentist")
    if args.limit < 1 or args.top < 1:
        p.error("--limit and --top must be at least 1")
    return args


def find_clinics(http, args):
    city = nominatim.find_city(http, args.city, args.country, args.region)
    if not city:
        sys.exit(f"Could not find '{args.city}' in '{args.country}'. Check the spelling, or try --region.")
    print(f"City: {city['display_name']}")
    if city["others"]:
        print(f"  (also matched: {'; '.join(city['others'][:3])} — use --region to pick another)")

    # Ask for extra rows: duplicates (point + building) get merged away after parsing.
    query = overpass.build_query(city, args.types, args.limit * 2, args.radius_km * 1000)
    try:
        data = overpass.run_query(http, query, refresh=args.refresh)
        source = "Overpass"
    except RuntimeError as exc:
        print(f"{exc}\nFalling back to Nominatim search (fewer results, max 120 per type).")
        data = nominatim.search_clinics(http, city, args.types, args.limit * 2)
        source = "Nominatim"
    clinics = overpass.parse_elements(data)[: args.limit]
    print(f"Found {len(clinics)} named clinics via {source} "
          f"({sum(1 for c in clinics if c['website'])} with a website on the map).\n")
    return clinics, city["name"]


def assess(http, robots, brave, clinic, city_name):
    """Fill in clinic['score'], ['issues'] and ['note']."""
    info = []
    if (not clinic["website"] or listing_host(clinic["website"])) and brave.enabled:
        found = brave.find_website(clinic["name"], city_name)
        if found:
            clinic["website"] = found
            info.append("website found by search but missing from OpenStreetMap")
    if clinic["website"]:
        try:
            result = check_website(http, robots, clinic["website"])
        except (requests.RequestException, ValueError) as exc:  # e.g. a bad URL in the map data
            result = {"issues": [("website_dead", type(exc).__name__)], "info": []}
        issues, info = result["issues"], info + result["info"]
        if any("checked the homepage instead" in i for i in info):
            clinic["website"] = result["url"]
        if not clinic["phone"] and result.get("phone"):
            clinic["phone"] = result["phone"]  # map had no phone; use the site's tel: link
    else:
        detail = "none on the map or in search" if brave.enabled else "none on the map, not verified"
        issues = [("no_website", detail)]
    clinic["score"] = scoring.score(issues)
    clinic["issues"] = scoring.issues_text(issues, info)
    clinic["note"] = scoring.note(issues, info)


def main(argv=None):
    load_dotenv()
    args = parse_args(argv)
    http = PoliteSession()
    robots = RobotsCache(http)
    brave = BraveLookup(http)

    print(f"Searching OpenStreetMap for {', '.join(args.types)} in {args.city}, {args.country}...")
    try:
        clinics, city_name = find_clinics(http, args)
    except requests.RequestException as exc:
        sys.exit(f"Could not reach OpenStreetMap services: {exc}")
    if not clinics:
        sys.exit("No named clinics found. Try a bigger --radius-km or different --types.")
    print("Brave fallback: " + ("on" if brave.enabled else "off (no BRAVE_API_KEY in .env)"))

    for i, clinic in enumerate(clinics, 1):
        print(f"[{i}/{len(clinics)}] {clinic['name']}", end=" ", flush=True)
        assess(http, robots, brave, clinic, city_name)
        print(f"-> score {clinic['score']}")

    rows = output.top_results(clinics, args.top)
    output.write_csv(rows, args.output)
    print(f"\nTop {len(rows)} of {len(clinics)} clinics (saved to {args.output}):\n")
    output.print_table(rows)
    print(f"\n{output.ATTRIBUTION}")
