# clinic-scout

Finds clinics, doctors and dentists in a city, checks their websites for common
marketing weaknesses, and exports the clinics most likely to need help to a CSV.

Everything it uses is free: OpenStreetMap data (Nominatim + Overpass) and each
clinic's own public homepage. No Google APIs, no paid APIs, no logins.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .            # or: pip install -r requirements.txt
cp .env.example .env        # optional settings, see below
```

## Usage

```bash
clinic-scout --city Bath --country "United Kingdom" --limit 20
# without installing: python -m clinic_scout --city Bath --country GB --limit 20
```

| Option | Default | Meaning |
|---|---|---|
| `--city` | required | City name |
| `--country` | required | Country name or 2-letter code (`GB`, `US`, `DE`) |
| `--region` | – | State/county, when the city name exists in several places |
| `--types` | `clinic,doctors,dentist` | OSM amenity types to search |
| `--limit` | `100` | Max clinics to check |
| `--top` | `15` | Rows to keep in the CSV |
| `--radius-km` | `5` | Search radius if the city has no boundary on the map |
| `--output` | `results.csv` | Output file |
| `--refresh` | off | Ignore cached OpenStreetMap results (cached for 24 h in `.cache/`) |

Output: `results.csv` with `name, phone, address, website, score, issues, note`,
plus a summary table in the terminal.

## How it works

1. **Find the city** with Nominatim, then **find clinics** inside its boundary with
   Overpass (`amenity=` and the newer `healthcare=` tags). If every Overpass server
   is down it falls back to a Nominatim search. Unnamed entries are skipped and the
   same clinic mapped twice (point + building) is merged.
2. **Check each website** (homepage only, 10 s timeout) and add points:

   | Weakness | Points |
   |---|---|
   | No own website (none on the map, or only a Facebook/NHS/directory page) | 10 |
   | Website down (DNS error, timeout, HTTP error, parked domain) | 10 |
   | No HTTPS | 2 |
   | No mobile viewport meta tag | 2 |
   | Homepage slower than 3 s | 1 |
   | No booking form/link, WhatsApp link or `tel:` link | 2 |
   | Copyright year more than 2 years old | 1 |
   | No Meta Pixel or Google Ads tags | 1 |
   | No Facebook or Instagram links | 2 |

   Weights live in `clinic_scout/scoring.py`. "No website" scores 10 rather than 5
   so clinics with no web presence rank above clinics that just have a weak site.

   Some sites are reported as "not checked" (0 points) rather than scored, so they
   don't get false scores: robots.txt disallows us, the site shows a bot-protection
   or captcha page, or the content is rendered by JavaScript (partial checks only).
   Sites using Google Tag Manager aren't penalised for missing ad tags, since
   GTM can load them invisibly.
3. **Optional Brave check.** If `BRAVE_API_KEY` is set in `.env`, clinics with no
   website on the map are searched on Brave (`"name" city`) before being scored,
   and a found website is checked like any other. Without a key this step is
   skipped and everything else works the same. A Brave Search API key needs a
   Brave account; check Brave's current plans for free usage limits.
4. **Export** the top `--top` clinics by score to CSV and print a summary.

## Good manners built in

- Descriptive User-Agent (add `CONTACT_EMAIL` in `.env` to include your contact).
- At most 1 request per second per domain, plus a 0.3 s gap between any requests.
- robots.txt is checked on clinic sites, including every redirect.
- Google, Facebook, Instagram and directory sites are never fetched, even via a redirect.
- OpenStreetMap results are cached for 24 hours.

## Settings (`.env`)

| Variable | Purpose |
|---|---|
| `BRAVE_API_KEY` | Enables the optional Brave lookup |
| `CONTACT_EMAIL` | Added to the User-Agent |
| `OVERPASS_URL` | Use one specific Overpass server |

`.env` is git-ignored; never commit keys.

## Tests

```bash
python -m unittest      # offline, no network needed
```

## Data licence and outreach

Clinic data © OpenStreetMap contributors, available under the
[ODbL](https://www.openstreetmap.org/copyright). Credit OpenStreetMap if you share
the results. Before contacting clinics, check local rules on unsolicited marketing
(e.g. GDPR/PECR in the UK and EU).
