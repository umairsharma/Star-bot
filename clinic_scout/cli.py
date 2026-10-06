"""Command-line entry point for clinic-scout."""

import argparse
import sys

from dotenv import load_dotenv

from clinic_scout.http_client import PoliteSession
from clinic_scout.overpass import find_clinics


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="clinic-scout",
        description="Find clinics in a city that likely need marketing help.",
    )
    p.add_argument("--city", required=True)
    p.add_argument("--country", required=True)
    p.add_argument("--types", default="clinic,doctors,dentist",
                   help="comma-separated OSM amenity values (default: %(default)s)")
    p.add_argument("--limit", type=int, default=100, help="max clinics to fetch (default: %(default)s)")
    p.add_argument("--top", type=int, default=15, help="rows to keep in results.csv (default: %(default)s)")
    return p.parse_args(argv)


def main(argv=None):
    load_dotenv()
    args = parse_args(argv)
    types = [t.strip() for t in args.types.split(",") if t.strip()]
    http = PoliteSession()

    print(f"Searching OpenStreetMap for {', '.join(types)} in {args.city}, {args.country}...")
    try:
        clinics = find_clinics(http, args.city, args.country, types, args.limit)
    except RuntimeError as exc:
        sys.exit(f"Error: {exc}")
    if not clinics:
        sys.exit("No named clinics found. Check the city/country spelling (use the OSM or English name).")

    with_site = sum(1 for c in clinics if c["website"])
    print(f"Found {len(clinics)} clinics ({with_site} with a website tag).\n")
    for c in clinics:
        print(f"- {c['name'][:40]:40} | {c['type']:8} | {c['phone'][:18]:18} | {c['website'] or '(no website)'}")
