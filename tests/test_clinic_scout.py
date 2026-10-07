"""Offline tests: no network needed. Run with `python -m unittest`."""

import datetime
import time
import unittest
from unittest import mock

from clinic_scout import nominatim, output, overpass, scoring
from clinic_scout.hosts import listing_host as hosts_listing_host, site_key
from clinic_scout.http_client import parse_robots, robots_allows
from clinic_scout.website_checks import decode_body
from clinic_scout.website_finder import ascii_words
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
    def __init__(self, status, text="", headers=None):
        self.status_code, self.text, self.content = status, text, text.encode("utf-8")
        self.headers = headers or {}
        self.is_redirect = status in (301, 302, 303, 307, 308) and "Location" in self.headers

    def close(self):
        pass


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
        # The real session would follow redirects itself (skipping our listing-host checks),
        # so every request must ask it not to.
        assert kwargs.get("allow_redirects") is False, f"{url} fetched with automatic redirects"
        self.requested.append(url)
        if url.endswith("/robots.txt") and url not in self.pages:
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


class ReviewFixTests(unittest.TestCase):
    """Regression tests for issues found in the pre-PR review."""

    def run_check(self, pages, website):
        web = FakeWeb(pages)
        return check_website(web, RobotsCache(web), website, today=TODAY), web.requested

    def test_robots_redirect_to_facebook_is_never_requested(self):
        fb = {"Location": "https://www.facebook.com/smileclinic"}
        pages = {"https://smileclinic.example/robots.txt": FakeResponse(301, headers=fb),
                 "https://smileclinic.example/": FakeSiteResponse(301, headers=fb)}
        result, requested = self.run_check(pages, "https://smileclinic.example/")
        self.assertEqual(result["issues"], [("no_website", "only a facebook.com page")])
        self.assertFalse(any("facebook" in u for u in requested), requested)

    def test_robots_rules_rfc9309(self):
        rules = parse_robots("\ufeffUser-agent: *\nDisallow: /private\nAllow: /private/ok\n"
                             "Disallow: /*.pdf$\n\nUser-agent: otherbot\nDisallow: /\n")
        self.assertFalse(robots_allows(rules, "https://a.example/private/x"))
        self.assertTrue(robots_allows(rules, "https://a.example/private/ok/page"))  # longest match wins
        self.assertFalse(robots_allows(rules, "https://a.example/files/a.pdf"))  # wildcard + end anchor
        self.assertTrue(robots_allows(rules, "https://a.example/files/a.pdf?x=1"))
        self.assertTrue(robots_allows(rules, "https://a.example/"))
        ours = parse_robots("User-agent: *\nDisallow: /\n\nUser-agent: clinic-scout\nAllow: /\n")
        self.assertTrue(robots_allows(ours, "https://a.example/page"))  # our own group wins over *

    def test_www_and_apex_share_a_rate_limit(self):
        self.assertEqual(site_key("https://www.clinic.example/a"), site_key("http://clinic.example/b"))

    def test_short_links_and_directories_are_listing_hosts(self):
        for url in ("https://g.co/kgs/AbC", "https://share.google/xyz", "https://m.me/clinic", "https://youtu.be/x",
                    "https://ig.me/x", "https://www.trustpilot.com/review/x", "https://maps.apple.com/?q=x",
                    "https://www.cqc.org.uk/location/1", "https://about.google/"):
            self.assertIsNotNone(hosts_listing_host(url), url)
        self.assertIsNone(hosts_listing_host("https://www.oldfieldsurgery.nhs.uk/"))
        self.assertIsNone(hosts_listing_host("http://[bad"))  # malformed URLs don't raise

    def test_unknown_charset_falls_back_instead_of_crashing(self):
        resp = FakeSiteResponse(200, headers={"Content-Type": "text/html; charset=windows-874"})
        resp.encoding = "windows-874-not-a-codec"
        self.assertEqual(decode_body("caf\u00e9".encode(), resp), "caf\u00e9")
        resp = FakeSiteResponse(200, headers={"Content-Type": "text/html"})
        body = '<meta charset="iso-8859-1"><p>\u00a9 2019</p>'.encode("iso-8859-1")
        self.assertIn("\u00a9 2019", decode_body(body, resp))  # <meta charset> is honoured

    def test_malformed_href_does_not_mark_site_dead(self):
        html = GOOD_SITE.replace("</body>", '<a href="http://[broken">x</a></body>')
        self.assertEqual(analyse_html(html, "https://smile.example/", 0.5, today=TODAY)["issues"], [])

    def test_copyright_ranges_to_present_or_two_digit_years(self):
        def codes(footer):
            html = WEAK_SITE.replace("Copyright 2019 Old Surgery", footer)
            return [c for c, _ in analyse_html(html, "https://x.example/", 0.5, today=TODAY)["issues"]]
        self.assertNotIn("old_copyright", codes("© 2015-present Old Surgery"))
        self.assertNotIn("old_copyright", codes("© 2015-25 Old Surgery"))
        self.assertIn("old_copyright", codes("© 2012-19 Old Surgery"))

    def test_facebook_widget_iframe_counts_as_social(self):
        html = WEAK_SITE.replace("</body>", '<iframe src="https://www.facebook.com/plugins/page.php?href=x"></iframe></body>')
        codes = [c for c, _ in analyse_html(html, "https://x.example/", 0.5, today=TODAY)["issues"]]
        self.assertNotIn("no_social", codes)

    def test_cloudflare_analytics_script_is_not_a_bot_page(self):
        page = GOOD_SITE.replace("</body>", '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></body>')
        result, _ = self.run_check({"https://clinic.example/": FakeSiteResponse(200, page)}, "https://clinic.example/")
        self.assertFalse(any("bot-protection" in i for i in result["info"]))

    def test_frameset_page_is_partly_checked(self):
        html = '<html><frameset><frame src="https://real-site.example/"></frameset></html>'
        result = analyse_html(html, "https://clinic.example/", 0.5, today=TODAY)
        self.assertNotIn("no_booking", [c for c, _ in result["issues"]])
        self.assertTrue(result["info"][0].startswith("partly checked"))
        self.assertEqual(scoring.note(result["issues"], result["info"])[:30], "Site isn't mobile-friendly.")

    def test_partly_checked_note(self):
        self.assertTrue(scoring.note([], ["partly checked: page content loads via JavaScript"]).startswith(
            "Website partly checked"))

    def test_finder_matches_whole_words_only(self):
        clinic = {"name": "Smile Dental", "postcode": "", "phone": "", "street": ""}
        self.assertFalse(page_matches("Smile Dental: a lovely practice", clinic, "Ely"))
        self.assertTrue(page_matches("Smile Dental, your dentist in Ely", clinic, "Ely"))

    def test_numeric_postcode_needs_the_city_too(self):
        clinic = {"name": "Harbour Dental", "postcode": "2000", "phone": "", "street": ""}
        self.assertFalse(page_matches("Established 2000. Call us today.", clinic, "Sydney"))
        self.assertTrue(page_matches("Level 2, George St, Sydney NSW 2000", clinic, "Sydney"))

    def test_accented_names_make_real_domain_guesses(self):
        self.assertEqual(ascii_words("Clínica Dental São Paulo"), ["clinica", "dental", "sao", "paulo"])
        self.assertIn("clinicadentalsaopaulo.com.br", domain_guesses("Clínica Dental São Paulo", "br"))

    def test_multi_value_website_prefers_own_site(self):
        data = {"elements": [{"type": "node", "id": 1, "lat": 1, "lon": 1, "tags": {
            "amenity": "dentist", "name": "X", "website": "https://facebook.com/x;https://x.example"}}]}
        self.assertEqual(overpass.parse_elements(data)[0]["website"], "https://x.example")

    def test_uk_country_code_alias(self):
        sent = []

        class Http:
            def get(self, url, params=None, **kwargs):
                sent.append(params)
                return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: []})()

        with mock.patch.object(nominatim.cache, "load", return_value=None), \
                mock.patch.object(nominatim.cache, "save") as save:
            nominatim.find_city(Http(), "Bath", "UK")
        self.assertEqual(sent[0]["countrycodes"], "gb")
        save.assert_not_called()  # empty answers aren't cached

    def test_brave_never_matches_on_empty_or_city_words(self):
        self.assertIsNone(pick_own_site("東京歯科", ["https://anything.example/"], "Tokyo"))
        self.assertIsNone(pick_own_site("Bristol Dental", ["https://www.bristol-council.example/"], "Bristol"))

    def test_missing_output_folder_fails_before_the_run(self):
        from clinic_scout import cli
        for bad in ("/no/such/folder/out.csv", "/tmp", "/tmp/"):
            with self.assertRaises(SystemExit, msg=bad):
                cli.parse_args(["--city", "Bath", "--country", "GB", "--output", bad])


class SecondReviewFixTests(unittest.TestCase):
    """Regression tests for issues found when re-reviewing the first round of fixes."""

    def test_robots_bom_bytes_without_charset_header(self):
        robots_txt = FakeResponse(200, headers={"Content-Type": "text/plain"})
        robots_txt.content = "\ufeffUser-agent: *\nDisallow: /\n".encode("utf-8")
        robots = RobotsCache(FakeHttp(robots_txt))
        self.assertFalse(robots.allowed("https://a.example/page"))

    def test_robots_path_params_and_percent_encoding(self):
        rules = parse_robots("User-agent: *\nDisallow: /*;jsessionid\nDisallow: /%7Ejoe/\nDisallow: /zahn%C3%A4rzte/\n")
        self.assertFalse(robots_allows(rules, "https://s.example/index.jsp;jsessionid=ABC"))
        self.assertFalse(robots_allows(rules, "https://s.example/~joe/x"))
        self.assertFalse(robots_allows(rules, "https://s.example/zahnärzte/"))
        self.assertTrue(robots_allows(rules, "https://s.example/"))

    def test_idn_hosts_share_a_rate_limit(self):
        self.assertEqual(site_key("https://zahnarzt-müller.de/"), site_key("https://www.xn--zahnarzt-mller-psb.de/"))
        self.assertEqual(site_key("https://Clinic.Example/"), "clinic.example")

    def test_finder_tries_www_after_bare_domain_error(self):
        page = GOOD_SITE.replace("Smile Dental", "Smile Dental Bath")
        web = FakeWeb({"https://www.smiledental.co.uk/": FakeSiteResponse(200, page)})  # bare domain: 404
        from clinic_scout.website_finder import WebsiteFinder
        finder = WebsiteFinder(web, RobotsCache(web), "gb")
        finder.use_dns = False
        clinic = {"name": "Smile Dental", "postcode": "", "phone": "", "street": ""}
        self.assertEqual(finder.find(clinic, "Bath"), "https://www.smiledental.co.uk/")

    def test_decode_body_bad_and_misleading_charsets(self):
        html = FakeSiteResponse(200, headers={"Content-Type": "text/html"})
        self.assertEqual(decode_body(b'<meta charset="undefined">ok', html)[-2:], "ok")
        self.assertEqual(decode_body(b'<meta charset="idna">ok', html)[-2:], "ok")
        self.assertEqual(decode_body('<meta charset="utf-16"><p>caf\u00e9</p>'.encode("utf-8"), html)[-8:],
                         "caf\u00e9</p>")  # <meta> claiming UTF-16 means UTF-8

    def test_copyright_tail_edge_cases(self):
        def codes(footer):
            html = WEAK_SITE.replace("Copyright 2019 Old Surgery", footer)
            return [c for c, _ in analyse_html(html, "https://x.example/", 0.5, today=TODAY)["issues"]]
        self.assertIn("old_copyright", codes("© 2015 - 12 High Street"))  # not a year range
        self.assertIn("old_copyright", codes("© 2015-presentation slides"))  # not "present"
        self.assertNotIn("old_copyright", codes("© 2015 - present"))

    def test_map_embed_does_not_make_a_page_framed(self):
        html = ('<html><body><p>Smile Dental</p><a href="tel:01225">Call</a>'
                '<iframe src="https://www.google.com/maps/embed?pb=x"></iframe></body></html>')
        result = analyse_html(html, "https://x.example/", 0.5, today=TODAY)
        self.assertFalse(any(i.startswith("partly checked") for i in result["info"]))

    def test_numeric_postcode_alone_when_city_has_no_latin_letters(self):
        clinic = {"name": "歯科", "postcode": "1500001", "phone": "", "street": ""}
        self.assertTrue(page_matches("東京都渋谷区 〒150-0001 1500001", clinic, "東京"))

    def test_brave_skips_unmatchable_names_without_a_query(self):
        from clinic_scout.brave import BraveLookup

        class NoCalls:
            def get(self, *a, **k):
                raise AssertionError("no API call expected")

        brave = BraveLookup(NoCalls())
        brave.enabled, brave.key = True, "test-key"
        self.assertEqual(brave.find_website("東京歯科", "Tokyo"), (None, False))


if __name__ == "__main__":
    unittest.main()
