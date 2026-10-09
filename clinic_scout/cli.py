"""Command-line entry point: find clinics, check their websites, score, export."""

import argparse
import os
import re
import sys

import requests
from dotenv import load_dotenv

from clinic_scout import discord, nominatim, output, overpass, scoring
from clinic_scout.brave import BraveLookup
from clinic_scout.http_client import PoliteSession, RobotsCache
from clinic_scout.hosts import clean_emails, listing_host
from clinic_scout.website_checks import check_website
from clinic_scout.website_finder import WebsiteFinder


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
    p.add_argument("--no-discord", action="store_true",
                   help="don't post lead cards to Discord even if DISCORD_WEBHOOK_URL is set")
    p.add_argument("--include-public", action="store_true",
                   help="keep public and hospital units (skipped by default: not marketing prospects)")
    args = p.parse_args(argv)
    args.types = [t.strip().lower() for t in args.types.split(",") if t.strip()]
    if not args.types or not all(re.fullmatch(r"[a-z_]+", t) for t in args.types):
        p.error("--types must be comma-separated words like clinic,doctors,dentist")
    if args.limit < 1 or args.top < 1:
        p.error("--limit and --top must be at least 1")
    # Fail now, not after checking every clinic.
    if args.output.endswith(("/", os.sep)) or os.path.isdir(args.output):
        p.error(f"--output must be a file path, not a folder: {args.output}")
    out_dir = os.path.dirname(os.path.abspath(args.output))
    if not os.path.isdir(out_dir):
        p.error(f"--output folder does not exist: {out_dir}")
    if not os.access(out_dir, os.W_OK):
        p.error(f"--output folder is not writable: {out_dir}")
    return args


def _check_order(clinic):
    """Clinics with their own website first (their checks are the most reliable),
    then listing-only links, then no website; better contact details first within each."""
    if clinic["website"] and not listing_host(clinic["website"]):
        group = 0
    else:
        group = 1 if clinic["website"] else 2
    return group, not (clinic["phone"] or clinic["postcode"]), clinic["name"].lower()


def find_clinics(http, args):
    """Return (clinics to check, city info)."""
    city = nominatim.find_city(http, args.city, args.country, args.region, refresh=args.refresh)
    if not city:
        sys.exit(f"Could not find '{args.city}' in '{args.country}'. Check the spelling, or try --region.")
    print(f"City: {city['display_name']}")
    if city["others"]:
        print(f"  (also matched: {'; '.join(city['others'][:3])} — use --region to pick another)")

    query = overpass.build_query(city, args.types, args.radius_km * 1000)
    try:
        data = overpass.run_query(http, query, refresh=args.refresh)
        source = "Overpass"
    except RuntimeError as exc:
        print(f"{exc}\nFalling back to Nominatim search (fewer results, max 120 per type).")
        data = nominatim.search_clinics(http, city, args.types, limit=10_000, refresh=args.refresh)
        source = "Nominatim"
    clinics = overpass.parse_elements(data, city["country_code"])
    print(f"Found {len(clinics)} named clinics via {source} "
          f"({sum(1 for c in clinics if c['website'])} with a website on the map).")
    public = [c for c in clinics if c["public"]]
    if public and not args.include_public:
        clinics = [c for c in clinics if not c["public"]]
        print(f"Skipped {len(public)} public or hospital units (use --include-public to keep them).")
    if len(clinics) > args.limit:
        print(f"Checking {args.limit} of {len(clinics)}, clinics with their own website first.")
    return sorted(clinics, key=_check_order)[: args.limit], city


def assess(http, robots, finder, brave, clinic, city_name):
    """Fill in clinic['score'], ['issues'], ['note'] and ['unverified']."""
    info = []
    listing = listing_host(clinic["website"]) if clinic["website"] else None
    if not clinic["website"] or listing:
        found, how, searched = finder.find(clinic, city_name), "by guessing its domain", False
        if not found and brave.enabled:
            candidate, searched = brave.find_website(clinic["name"], city_name)
            how = "via Brave search"
            if candidate:  # only trust it if the page names the clinic (postcode, phone, or name + city)
                found = finder.verify_url(candidate, clinic, city_name)
                searched = bool(found)  # an unverifiable result doesn't confirm "no website" either
        if found:
            clinic["website"] = found
            info.append(f"website found {how}; missing from OpenStreetMap")
        else:
            where = f"map links only to a {listing} page" if listing else "none on the map"
            if searched:
                issues = [("no_website", f"{where}; none at guessed domains or in Brave search")]
            else:
                issues = [("no_website_unverified", f"{where}; none at guessed domains")]
            return _finish(clinic, issues, info)

    try:
        result = check_website(http, robots, clinic["website"])
    except requests.RequestException as exc:  # e.g. a bad URL in the map data
        result = {"issues": [("website_dead", type(exc).__name__)], "info": []}
    dead = dict(result["issues"]).get("website_dead")
    if dead and not info:  # the map's link is dead; the clinic may have moved to a new domain
        found = finder.find(clinic, city_name)
        if found and found.rstrip("/") != clinic["website"].rstrip("/"):
            info.append(f"map link is dead ({dead}); current site found by guessing its domain")
            clinic["website"] = found
            result = check_website(http, robots, found)
    info += result["info"]
    # Contacts published on their own site, added to any from the map.
    clinic["emails"] = clean_emails(clinic.get("emails", []) + result.get("emails", []))
    clinic["socials"] = {**result.get("socials", {}), **clinic.get("socials", {})}  # map entries win
    if any("checked the homepage instead" in i for i in info):
        clinic["website"] = result["url"]
    if not clinic["phone"] and result.get("phone"):
        clinic["phone"] = result["phone"]  # map had no phone; use the site's tel: link
    return _finish(clinic, result["issues"], info)


def _finish(clinic, issues, info):
    for key, empty in (("emails", []), ("socials", {}), ("listing_url", ""), ("osm_url", "")):
        clinic.setdefault(key, empty)
    clinic["score"] = scoring.score(issues)
    clinic["issues"] = scoring.issues_text(issues, info)
    clinic["note"] = scoring.note(issues, info)
    clinic["unverified"] = any(code == "no_website_unverified" for code, _ in issues)


def main(argv=None):
    load_dotenv()
    args = parse_args(argv)
    http = PoliteSession()
    robots = RobotsCache(http)
    brave = BraveLookup(http)

    print(f"Searching OpenStreetMap for {', '.join(args.types)} in {args.city}, {args.country}...")
    try:
        clinics, city = find_clinics(http, args)
    except requests.RequestException as exc:
        sys.exit(f"Could not reach OpenStreetMap services: {exc}")
    if not clinics:
        hint = "different --types" if city["osm_type"] == "relation" else "a bigger --radius-km or different --types"
        sys.exit(f"No named clinics found. Try {hint}.")
    finder = WebsiteFinder(http, robots, city["country_code"])
    print("Brave fallback: " + ("on" if brave.enabled else "off (no BRAVE_API_KEY in .env)") + "\n")

    for i, clinic in enumerate(clinics, 1):
        print(f"[{i}/{len(clinics)}] {clinic['name']}", end=" ", flush=True)
        try:
            assess(http, robots, finder, brave, clinic, city["name"])
        except Exception as exc:  # one odd site must never cost the whole run
            _finish(clinic, [], [f"not checked: unexpected error ({type(exc).__name__})"])
        print(f"-> score {clinic['score']}")

    rows = output.top_results(clinics, args.top)
    output.write_csv(rows, args.output)
    print(f"\nTop {len(rows)} of {len(clinics)} clinics (saved to {args.output}):\n")
    output.print_table(rows)
    found = sum(1 for c in clinics if "missing from OpenStreetMap" in c["issues"])
    unverified = sum(1 for c in clinics if c["unverified"])
    if found:
        print(f"\nFound {found} websites that are missing from OpenStreetMap, and checked them.")
    if unverified:
        if brave.enabled:
            hint = ""
        elif brave.key:
            hint = " The Brave check was turned off during the run (key rejected or over quota)."
        else:
            hint = " Add BRAVE_API_KEY to .env to confirm them."
        print(f"{unverified} clinics have no website on the map or at likely domains. They score "
              f"{scoring.WEIGHTS['no_website_unverified']} (unverified), so sites with bigger confirmed "
              f"problems rank above them.{hint}")
    print(f"\n{output.ATTRIBUTION}")
    post_to_discord(http, args, rows, city, len(clinics))


def _city_label(city):
    """"Dallas, Dallas County, Texas, United States" -> "Dallas, Texas, United States"."""
    parts = city["display_name"].split(", ")
    return ", ".join(dict.fromkeys([parts[0]] + parts[-2:]))


def post_to_discord(http, args, rows, city, checked):
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if args.no_discord:
        return
    if not webhook:
        print("Discord: off (add DISCORD_WEBHOOK_URL to .env to post one card per lead).")
        return
    if not discord.is_webhook_url(webhook):
        print("Discord: DISCORD_WEBHOOK_URL doesn't look like a Discord webhook URL; nothing posted.")
        return
    posted = discord.post_leads(http, webhook, rows, _city_label(city), checked)
    print(f"Discord: posted {posted} of {len(rows)} lead cards.")
