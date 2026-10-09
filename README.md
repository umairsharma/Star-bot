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
| `--country` | required | Country name or 2-letter code (`GB`, `US`, `DE`; `UK` also works) |
| `--region` | – | State/county, when the city name exists in several places |
| `--types` | `clinic,doctors,dentist` | OSM amenity types to search |
| `--limit` | `100` | Max clinics to check |
| `--top` | `15` | Rows to keep in the CSV |
| `--radius-km` | `5` | Search radius if the city has no boundary on the map |
| `--output` | `results.csv` | Output file |
| `--refresh` | off | Ignore cached OpenStreetMap results (Nominatim and Overpass, cached for 24 h in `.cache/`) |
| `--include-public` | off | Keep public and hospital units (skipped by default) |
| `--no-discord` | off | Don't post lead cards to Discord even if `DISCORD_WEBHOOK_URL` is set |

Output: `results.csv` with `name, phone, address, website, score, issues, note`,
plus a summary table in the terminal.

## Run it from your phone (no computer needed)

The repo includes a GitHub Actions workflow, so GitHub's servers can run it for free:

1. Open the repo on github.com in your phone's browser and tap the **Actions** tab.
2. Choose **Run clinic-scout**, then **Run workflow**.
3. Enter the city and country (and optionally a state/region, limit and top), then tap
   **Run workflow**.
4. When the run finishes (about 15 minutes for 100 clinics), open it: the results
   table is on the run's summary page, and `results.csv` is under **Artifacts**.

To enable the optional Brave check there, add `BRAVE_API_KEY` as a repository secret
(Settings → Secrets and variables → Actions). GitHub Actions is free for public
repos; private repos on the Free plan get a monthly allowance of free minutes.

## Post leads to Discord

Each lead can be posted to a Discord channel as its own card. Every card includes:
- **Details:** score, the note, phone, email, address, website and what's wrong.
- **Social media:** accounts from the clinic's website and the map data.
- **Where they're listed:** their OpenStreetMap entry, plus any directory page the map links to.
- **Lookups:** "Search on Google" (normally shows their Google business panel) and "Open in Google Maps".
- **"Share on WhatsApp":** a pre-filled message with the lead's details.

The Google, Maps and WhatsApp entries are plain links on the card. clinic-scout never visits those sites itself.

Setup (works from a phone):
1. **In Discord:** long-press your channel, tap **Edit Channel**, then **Integrations**, **Webhooks**, **New Webhook**. Copy its URL.
2. **On github.com:** open the repo, then **Settings**, **Secrets and variables**, **Actions**, **New repository secret**. Name it `DISCORD_WEBHOOK_URL` and paste the URL.
3. **Run it:** use **Run workflow** as above. Untick "Post to Discord" for a run you don't want posted.

Running on a computer instead, put `DISCORD_WEBHOOK_URL=...` in `.env`. Use `--no-discord` to skip posting. Treat the webhook URL like a password: anyone who has it can post to your channel.

## How it works

1. **Find the city** with Nominatim, then fetch **every clinic** inside its boundary
   from Overpass (`amenity=` and the newer `healthcare=` tags). If every Overpass
   server is down it falls back to a Nominatim search. Unnamed entries are skipped
   and the same clinic mapped twice (point + building) is merged.
2. **Pick which clinics to check.** Public and hospital units are skipped: names with
   Hospital/NHS/Infirmary, hospital or government-run entries, and university clinics.
   In the UK, NHS walk-in centres, NHS trusts and hospital-only specialities are skipped
   too (elsewhere, e.g. the US, those are often private businesses, so they're kept).
   Then up to `--limit` clinics are checked, those with their own website first,
   since their results are the most reliable.
3. **Find missing websites for free.** For clinics with no website on the map (or
   only a listing page), likely domains are guessed from the name
   (`ashleydowndentalcare.co.uk`, `ashleydowndental.co.uk`, …). Each guess gets a
   DNS lookup first, and a page only counts if it mentions the clinic's postcode,
   phone number, or its name together with the city. Found sites are checked like
   any other.
4. **Check each website** (homepage only, 10 s timeout) and add points:

   | Weakness | Points |
   |---|---|
   | No own website, confirmed by Brave search too | 10 |
   | No website on the map or at guessed domains (unverified) | 3 |
   | Website down (DNS error, timeout, HTTP error, parked domain) | 10 |
   | No HTTPS | 2 |
   | No mobile viewport meta tag | 2 |
   | Homepage slower than 3 s | 1 |
   | No booking form/link, WhatsApp link or `tel:` link | 2 |
   | Copyright year more than 2 years old | 1 |
   | No Meta Pixel or Google Ads tags | 1 |
   | No Facebook or Instagram links | 2 |

   Weights live in `clinic_scout/scoring.py`. OpenStreetMap often lacks websites
   that do exist, so an unconfirmed "no website" scores only 3: sites with bigger
   confirmed problems rank above it. At equal scores, checked sites rank above
   unverified ones. A failed fetch is retried once before a site counts as down.

   Some sites aren't scored, so they don't get false scores. They're "not checked"
   (0 points) when robots.txt disallows us or the site shows a bot-protection or
   captcha page, and "partly checked" (only HTTPS, mobile and speed are scored)
   when the content is rendered by JavaScript or shown inside a frame.
   Sites using Google Tag Manager aren't penalised for missing ad tags, since
   GTM can load them invisibly.
5. **Optional Brave check.** If `BRAVE_API_KEY` is set in `.env`, clinics still
   without a website after the domain guess are searched on Brave (`"name" city`).
   Nothing found there confirms "no website" (10 points). A found site must pass
   the same postcode/phone/name check as a guessed domain, then is checked like
   any other. Without a key this step is skipped and everything else works. A Brave Search API key needs a
   Brave account; check Brave's current plans for free usage limits.
6. **Export** the top `--top` clinics by score to CSV and print a summary.

## Good manners built in

- Descriptive User-Agent (add `CONTACT_EMAIL` in `.env` to include your contact).
- At most 1 request per second per domain, plus a 0.3 s gap between any requests.
- robots.txt is checked on clinic sites, including every redirect, using RFC 9309
  rules (our own `clinic-scout` group first, longest match wins, `*` and `$` wildcards).
- Google, Facebook, Instagram, their short links (`g.co`, `m.me`, …) and directory
  sites are never fetched, not even through a redirect or a robots.txt request.
  The list is in `clinic_scout/hosts.py`.
- One odd site (bad charset, malformed links) can't stop the run; it's marked
  "not checked" and the run carries on.
- OpenStreetMap results are cached for 24 hours.

## Settings (`.env`)

| Variable | Purpose |
|---|---|
| `BRAVE_API_KEY` | Enables the optional Brave lookup |
| `DISCORD_WEBHOOK_URL` | Posts one card per lead to that Discord channel |
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
