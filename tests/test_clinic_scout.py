"""Offline tests: no network needed. Run with `python -m unittest`."""

import datetime
import time
import unittest

from clinic_scout import output, overpass, scoring
from clinic_scout.website_finder import domain_guesses, page_matches
from clinic_scout.brave import pick_own_site
from clinic_scout.http_client import RobotsCache
from clinic_scout.website_checks import analyse_html, check_website, listing_host

TODAY = datetime.date(2026, 10, 6)
FILLER = "<p>" + "We offer friendly family dentistry. " * 20 + "</p>"

GOOD_SITE = f"""<html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>!function(f,b,e,v,n,t,s){{}}(window,document,'script',
'https://connect.facebook.net/en_US/fbevents.js'); fbq('init', '123');</script>
</head><body>{FILLER}
<a href="tel:+441225000000">Call us</a>
<a href="https://www.facebook.com/smiledental">Facebook</a>
<footer>© 2015-2026 Smile Dental</footer></body></html>"""

WEAK_SITE = f"""<html><head><title>Old Surgery</title></head><body>{FILLER}
<a href="/about">About us</a><footer>Copyright 2019 Old Surgery</footer></body></html>"""


class AnalyseHtmlTests(unittest.TestCase):
    def codes(self, html, url="https://smile.example/", seconds=0.5):
        return [code for code, _ in analyse_html(html, url, seconds, today=TODAY)["issues"]]

    def test_good_site_has_no_issues(self):
        self.assertEqual(self.codes(GOOD_SITE), [])

    def test_phone_taken_from_tel_link(self):
        self.assertEqual(analyse_html(GOOD_SITE, "https://smile.example/", 0.5, today=TODAY)["phone"],
                         "+441225000000")

    def test_weak_site_flags_everything(self):
        codes = self.codes(WEAK_SITE, url="http://old.example/", seconds=4.2)
        self.assertEqual(codes, ["no_https", "no_viewport", "slow", "no_booking",
                                 "old_copyright", "no_ads", "no_social"])

    def test_copyright_two_years_old_is_fine(self):
        self.assertNotIn("old_copyright", self.codes(WEAK_SITE.replace("2019", "2024")))

    def test_booking_link_text_counts(self):
        html = WEAK_SITE.replace('<a href="/about">About us</a>', '<a href="/contact">Book an appointment</a>')
        self.assertNotIn("no_booking", self.codes(html))

    def test_booking_platform_iframe_counts(self):
        html = WEAK_SITE.replace("</body>", '<iframe src="https://smile.portal.dentally.co/"></iframe></body>')
        self.assertNotIn("no_booking", self.codes(html))

    def test_whatsapp_link_counts(self):
        html = WEAK_SITE.replace('href="/about"', 'href="https://wa.me/441225000000"')
        self.assertNotIn("no_booking", self.codes(html))

    def test_facebook_in_path_is_not_a_booking_word(self):
        html = WEAK_SITE.replace('href="/about"', 'href="https://www.facebook.com/x"')
        codes = self.codes(html)
        self.assertIn("no_booking", codes)
        self.assertNotIn("no_social", codes)

    def test_tag_manager_is_not_penalised(self):
        html = WEAK_SITE.replace("</head>", "<script src='https://www.googletagmanager.com/gtm.js?id=GTM-AB12'></script></head>")
        result = analyse_html(html, "https://x.example/", 0.5, today=TODAY)
        self.assertNotIn("no_ads", [c for c, _ in result["issues"]])
        self.assertTrue(any("Tag Manager" in i for i in result["info"]))

    def test_javascript_rendered_page_skips_content_checks(self):
        html = '<html><head><script src="/app.js"></script></head><body><div id="root"></div></body></html>'
        result = analyse_html(html, "https://spa.example/", 0.5, today=TODAY)
        self.assertEqual([c for c, _ in result["issues"]], ["no_viewport"])
        self.assertTrue(result["info"])

    def test_parked_domain_counts_as_dead(self):
        html = "<html><body>This domain is for sale! Buy this domain today.</body></html>"
        self.assertEqual(self.codes(html), ["website_dead"])


class OverpassTests(unittest.TestCase):
    def test_query_uses_area_for_relations_and_radius_otherwise(self):
        rel = {"osm_type": "relation", "osm_id": 5342409, "lat": 51.38, "lon": -2.36}
        q = overpass.build_query(rel, ["clinic", "doctors"], 5000)
        self.assertIn("area(3605342409)", q)
        self.assertIn('"healthcare"~"^(clinic|doctor)$"', q)
        self.assertIn("out tags center;", q)  # whole city, no cap
        node = dict(rel, osm_type="node")
        self.assertIn("around:5000,51.38,-2.36", overpass.build_query(node, ["dentist"], 5000))

    def test_public_units(self):
        self.assertTrue(overpass.is_public_unit({"name": "Bristol City Gate NHS walk-in centre"}))
        self.assertTrue(overpass.is_public_unit({"name": "Bristol Eye Hospital Assessment Clinic"}))
        self.assertTrue(overpass.is_public_unit({"name": "Petherton Resource Centre",
                                                 "operator": "Avon and Wiltshire Mental Health Partnership NHS Trust"}))
        self.assertTrue(overpass.is_public_unit({"name": "Breast Care Centre", "healthcare:speciality": "oncology"}))
        self.assertFalse(overpass.is_public_unit({"name": "Prime Endoscopy Bristol", "healthcare:speciality": "endoscopy"}))
        self.assertFalse(overpass.is_public_unit({"name": "Circus Dental", "operator": "Bupa"}))

    def test_parse_skips_unnamed_and_merges_nearby_duplicates(self):
        data = {"elements": [
            {"type": "node", "id": 1, "lat": 51.3800, "lon": -2.3600,
             "tags": {"amenity": "dentist", "name": "Circus Dental", "phone": "01225 1"}},
            {"type": "way", "id": 2, "center": {"lat": 51.3801, "lon": -2.3601},
             "tags": {"amenity": "dentist", "name": "Circus Dental", "website": "https://circus.example"}},
            {"type": "node", "id": 3, "lat": 51.4000, "lon": -2.3000,
             "tags": {"amenity": "dentist", "name": "Circus Dental"}},  # 3 km away: a different branch
            {"type": "node", "id": 4, "lat": 51.38, "lon": -2.36, "tags": {"amenity": "clinic"}},
            {"type": "node", "id": 5, "lat": 51.38, "lon": -2.36,
             "tags": {"healthcare": "doctor", "name": "Abbey Surgery", "contact:phone": "01225 2",
                      "addr:housenumber": "1", "addr:street": "High St", "addr:city": "Bath"}},
        ]}
        clinics = overpass.parse_elements(data)
        self.assertEqual([c["name"] for c in clinics], ["Circus Dental", "Circus Dental", "Abbey Surgery"])
        self.assertEqual(clinics[0]["website"], "https://circus.example")  # merged from the building
        self.assertEqual(clinics[0]["phone"], "01225 1")
        self.assertEqual(clinics[2]["address"], "1 High St, Bath")
        self.assertEqual(clinics[2]["type"], "doctor")


class ScoringTests(unittest.TestCase):
    def test_score_and_note(self):
        issues = [("no_https", ""), ("no_viewport", ""), ("slow", "4.2s"), ("no_social", "")]
        self.assertEqual(scoring.score(issues), 7)
        self.assertEqual(scoring.note(issues),
                         "Site has no HTTPS, isn't mobile-friendly and has no social media links (+1 more issue).")
        self.assertEqual(scoring.issues_text(issues),
                         "no HTTPS; not mobile-friendly (no viewport tag); slow homepage (4.2s); "
                         "no Facebook or Instagram links")

    def test_confirmed_problems_outrank_unverified_no_website(self):
        weak_site = scoring.score([(c, "") for c in ("no_https", "no_viewport", "no_booking", "no_social")])
        self.assertGreater(scoring.score([("no_website", "none anywhere")]), weak_site)
        self.assertLess(scoring.score([("no_website_unverified", "none on the map")]), weak_site)

    def test_unverified_ranks_after_checked_site_with_same_score(self):
        rows = [{"name": "A", "score": 3, "unverified": True}, {"name": "B", "score": 3, "unverified": False},
                {"name": "C", "score": 5, "unverified": False}]
        self.assertEqual([r["name"] for r in output.top_results(rows, 3)], ["C", "B", "A"])

    def test_no_website_notes(self):
        self.assertEqual(scoring.note([("no_website_unverified", "map links only to a www.nhs.uk page; none at guessed domains")]),
                         "Map links only to a www.nhs.uk page and none found at likely domains; worth a quick manual check.")
        self.assertEqual(scoring.note([("no_website", "only a facebook.com page")]),
                         "Their only web presence is a facebook.com page, so they're hard to find and book online.")

    def test_info_only_note(self):
        self.assertEqual(scoring.note([], ["not checked: robots.txt disallows it"]),
                         "Website not checked: robots.txt disallows it.")


class WebsiteFinderTests(unittest.TestCase):
    def test_domain_guesses(self):
        guesses = domain_guesses("Ashley Down Dental Care", "gb")
        self.assertEqual(guesses[:3], ["ashleydowndentalcare.co.uk", "ashleydowndentalcare.com", "ashleydowndentalcare.uk"])
        self.assertIn("ashleydowndental.co.uk", guesses)
        self.assertIn("ashley-down-dental-care.co.uk", guesses)
        self.assertIn("cliftonpractice.co.uk", domain_guesses("The Clifton Practice", "gb"))
        self.assertIn("smithandjones.com", domain_guesses("Smith & Jones", "us"))

    def test_page_must_mention_postcode_phone_or_name_and_city(self):
        clinic = {"name": "Ashley Down Dental Care", "postcode": "BS7 9BL", "phone": "0117 924 5555", "street": ""}
        self.assertTrue(page_matches("Visit us at 1 Station Rd, Bristol BS7 9BL", clinic, "Bristol"))
        self.assertTrue(page_matches("Call 0117 9245555 today", clinic, "Bristol"))
        self.assertTrue(page_matches("Ashley Down Dental Care - your dentist in Bristol", clinic, "Bristol"))
        self.assertFalse(page_matches("Ashley Down Dental Care, Leeds", clinic, "Bristol"))
        self.assertFalse(page_matches("A dentist in Bristol", clinic, "Bristol"))


class AssessTests(unittest.TestCase):
    """cli.assess with fake web access: the finder runs for missing and dead websites."""

    class Finder:
        def __init__(self, result):
            self.result = result

        def find(self, clinic, city):
            return self.result

    class NoBrave:
        enabled = False

    def assess(self, website, pages, finder_result):
        from clinic_scout import cli
        clinic = {"name": "Smile Dental", "website": website, "phone": "", "postcode": ""}
        web = FakeWeb(pages)
        cli.assess(web, RobotsCache(web), self.Finder(finder_result), self.NoBrave(), clinic, "Bath")
        return clinic

    def test_missing_website_unverified_scores_3(self):
        clinic = self.assess("", {}, None)
        self.assertEqual((clinic["score"], clinic["unverified"]), (3, True))

    def test_found_website_is_checked(self):
        clinic = self.assess("", {"https://smile.example/": FakeSiteResponse(200, GOOD_SITE)}, "https://smile.example/")
        self.assertEqual((clinic["score"], clinic["website"]), (0, "https://smile.example/"))
        self.assertIn("missing from OpenStreetMap", clinic["issues"])

    def test_dead_map_link_with_new_site_checks_the_new_site(self):
        pages = {"https://smile.example/": FakeSiteResponse(200, GOOD_SITE)}
        clinic = self.assess("https://old-smile.example/", pages, "https://smile.example/")
        self.assertEqual((clinic["score"], clinic["website"]), (0, "https://smile.example/"))
        self.assertIn("map link is dead (HTTP 404)", clinic["issues"])

    def test_dead_map_link_without_new_site_stays_dead(self):
        clinic = self.assess("https://old-smile.example/", {}, None)
        self.assertEqual(clinic["score"], 10)


class BraveTests(unittest.TestCase):
    def test_skips_directories_and_matches_name(self):
        urls = ["https://www.yelp.co.uk/biz/circus-dental", "https://www.facebook.com/circusdental",
                "https://www.circusdental.co.uk/contact"]
        self.assertEqual(pick_own_site("Circus Dental", urls), "https://www.circusdental.co.uk/")

    def test_no_match_returns_none(self):
        self.assertIsNone(pick_own_site("Circus Dental", ["https://www.othersite.com/"]))


class FakeResponse:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class FakeHttp:
    def __init__(self, response):
        self.response, self.calls = response, 0

    def get(self, url, **kwargs):
        self.calls += 1
        return self.response


class RobotsTests(unittest.TestCase):
    def test_rules_are_respected_and_cached(self):
        http = FakeHttp(FakeResponse(200, "User-agent: *\nDisallow: /private\n"))
        robots = RobotsCache(http)
        self.assertTrue(robots.allowed("https://a.example/"))
        self.assertFalse(robots.allowed("https://a.example/private/x"))
        self.assertEqual(http.calls, 1)

    def test_missing_robots_allows_and_server_error_disallows(self):
        self.assertTrue(RobotsCache(FakeHttp(FakeResponse(404))).allowed("https://a.example/"))
        self.assertFalse(RobotsCache(FakeHttp(FakeResponse(503))).allowed("https://a.example/"))


class FakeSiteResponse:
    def __init__(self, status, body="", headers=None):
        self.status_code, self.body = status, body.encode()
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        self.encoding, self.started_at = "utf-8", 0.0
        self.is_redirect = status in (301, 302, 303, 307, 308) and "Location" in self.headers

    def iter_content(self, size):
        yield self.body

    def close(self):
        pass


class FakeWeb:
    """Serves canned pages by URL and records every URL requested."""

    def __init__(self, pages):
        self.pages, self.requested = pages, []

    def get(self, url, **kwargs):
        self.requested.append(url)
        if url.endswith("/robots.txt"):
            return FakeResponse(404)
        resp = self.pages.get(url) or FakeSiteResponse(404)
        resp.started_at = time.monotonic()
        return resp


class CheckWebsiteTests(unittest.TestCase):
    def run_check(self, pages, website):
        web = FakeWeb(pages)
        return check_website(web, RobotsCache(web), website, today=TODAY), web.requested

    def test_social_link_is_no_website_and_never_fetched(self):
        result, requested = self.run_check({}, "https://www.facebook.com/somedentist")
        self.assertEqual(result["issues"], [("no_website", "only a facebook.com page")])
        self.assertEqual(requested, [])

    def test_redirect_to_facebook_is_not_followed(self):
        pages = {"https://clinic.example/": FakeSiteResponse(301, headers={"Location": "https://facebook.com/clinic"})}
        result, requested = self.run_check(pages, "https://clinic.example/")
        self.assertEqual(result["issues"], [("no_website", "only a facebook.com page")])
        self.assertFalse(any("facebook" in u for u in requested))

    def test_bot_protection_page_is_not_scored(self):
        captcha = '<html><head><meta http-equiv="refresh" content="0;/.well-known/sgcaptcha/?r=%2F"></head></html>'
        result, _ = self.run_check({"https://clinic.example/": FakeSiteResponse(202, captcha)}, "https://clinic.example/")
        self.assertEqual(result["issues"], [])
        self.assertIn("bot-protection", result["info"][0])

    def test_meta_refresh_is_followed(self):
        pages = {
            "https://clinic.example/": FakeSiteResponse(200, '<meta http-equiv="Refresh" content="0; URL=/home">'),
            "https://clinic.example/home": FakeSiteResponse(200, GOOD_SITE),
        }
        result, _ = self.run_check(pages, "https://clinic.example/")
        self.assertEqual(result["url"], "https://clinic.example/home")
        self.assertEqual(result["issues"], [])

    def test_dead_deep_link_falls_back_to_homepage(self):
        pages = {"https://clinic.example/": FakeSiteResponse(200, GOOD_SITE)}
        result, _ = self.run_check(pages, "https://clinic.example/old/contact.aspx")
        self.assertEqual(result["issues"], [])
        self.assertIn("map link is broken (HTTP 404)", result["info"][0])

    def test_whole_site_404_is_dead(self):
        result, _ = self.run_check({}, "https://clinic.example/")
        self.assertEqual(result["issues"], [("website_dead", "HTTP 404")])

    def test_listing_host(self):
        self.assertEqual(listing_host("https://www.google.co.uk/maps/place/x"), "google.com")
        self.assertEqual(listing_host("https://www.nhs.uk/services/gp-surgery/x"), "www.nhs.uk")
        self.assertIsNone(listing_host("https://www.oldfieldsurgery.nhs.uk/"))
        self.assertIsNone(listing_host("https://googleclinic.example/"))


if __name__ == "__main__":
    unittest.main()
