"""Turn results.csv into a Markdown table for the GitHub Actions run summary page."""

import csv
import os
import sys


def cell(text):
    return (text or "").replace("|", "\\|").replace("\n", " ").strip() or "-"


def main(path):
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    city = os.getenv("CITY", "").strip()
    print(f"## Top {len(rows)} clinics{' in ' + city if city else ''}\n")
    print("| # | Score | Clinic | Phone | Website | What's wrong |")
    print("|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        print(f"| {i} | {cell(r['score'])} | {cell(r['name'])} | {cell(r['phone'])} | "
              f"{cell(r['website'])} | {cell(r['note'])} |")
    print("\nThe full CSV (with the `issues` column) is under **Artifacts** below.")
    print("\nClinic data © OpenStreetMap contributors, available under the ODbL.")


if __name__ == "__main__":
    main(sys.argv[1])
