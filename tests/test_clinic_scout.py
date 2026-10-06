"""Offline tests: no network needed. Run with `python -m unittest`."""

import datetime
import time
import unittest

from clinic_scout import overpass, scoring
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
        q = overpass.build_query(rel, ["clinic", "doctors"], 20, 5000)
        self.assertIn("area(3605342409)", q)
        self.assertIn('"healthcare"~"^(clinic|doctor)$"', q)
        node = dict(rel, osm_type="node")
        self.assertIn("around:5000,51.38,-2.36", overpass.build_query(node, ["dentist"], 20, 5000))

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

    def test_no_website_outranks_weak_website(self):
        self.assertGreater(scoring.score([("no_website", "unverified")]),
                           scoring.score([(c, "") for c in ("no_https", "no_viewport", "no_booking", "no_social")]))

    def test_info_only_note(self):
        self.assertEqual(scoring.note([], ["not checked: robots.txt disallows it"]),
                         "Website not checked: robots.txt disallows it.")


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
