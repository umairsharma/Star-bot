"""Write results.csv and print a summary table."""

import csv
import shutil

CSV_COLUMNS = ["name", "phone", "address", "website", "score", "issues", "note"]
ATTRIBUTION = "Clinic data © OpenStreetMap contributors, available under the ODbL (openstreetmap.org/copyright)."


def top_results(clinics, top):
    return sorted(clinics, key=lambda c: (-c["score"], c["name"].lower()))[:top]


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _cut(text, width):
    return text if len(text) <= width else text[: width - 1] + "…"


def print_table(rows):
    name_w, site_w = 32, 30
    header = f"{'#':>3}  {'Score':>5}  {'Name':<{name_w}}  {'Website':<{site_w}}  Note"
    print(header)
    print("-" * shutil.get_terminal_size((120, 20)).columns)
    for i, r in enumerate(rows, 1):
        site = r["website"].replace("https://", "").replace("http://", "").rstrip("/") or "-"
        print(f"{i:>3}  {r['score']:>5}  {_cut(r['name'], name_w):<{name_w}}  "
              f"{_cut(site, site_w):<{site_w}}  {r['note']}")
