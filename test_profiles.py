"""Behavior tests for multi-profile filtering, WBM auto-apply and scrapers."""

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

import appliers.wbm as wbm_applier
import flat_monitor
import monitor_dispatch
from profile_config import (
    Profile,
    is_senior_housing,
    is_student_housing,
    load_profiles,
    matches_profile,
)


def sample_flat(**overrides):
    base = {
        "flat_id": "degewo_101",
        "object_id": "101",
        "fetcher": "degewo",
        "source": "degewo",
        "title": "Helle 2-Zimmer-Wohnung",
        "description": "Schöne ruhige Wohnung mit Balkon",
        "address": "Sonnenallee 10, 12045 Berlin",
        "district": "Neukölln",
        "rooms": 2.0,
        "area": 48.0,
        "kaltmiete": 400.0,
        "warmmiete": 520.0,
        "wbs_required": True,
        "raw_text": "WBS 100 erforderlich",
        "url": "https://immosuche.degewo.de/de/properties/101",
    }
    base.update(overrides)
    return base


class FilterUnitTests(unittest.TestCase):
    def setUp(self):
        self.profile_primary = Profile(
            id="primary",
            display_name="Primary",
            enabled=True,
            allow_senior=False,
            allow_student=False,
            rooms_min=1.0,
            rooms_max=2.0,
            size_max_m2=50.0,
            warm_rent_max=600.0,
            cold_rent_max=460.0,
        )
        self.profile_secondary = Profile(
            id="secondary",
            display_name="Secondary",
            enabled=True,
            allow_senior=False,
            allow_student=False,
            rooms_min=1.0,
            rooms_max=2.0,
            size_max_m2=65.0,
            warm_rent_max=650.0,
            cold_rent_max=550.0,
            wbs_required_ok=True,
            wbs_categories_ok=["100"],
        )

    def test_senior_filtering(self):
        senior_cases = [
            sample_flat(title="Schöne Seniorenwohnung im Grünen"),
            sample_flat(title="2-Zimmer ab 60 Jahren"),
            sample_flat(title="Wohnung für Ruheständler"),
            sample_flat(description="Ruhige Wohnanlage ab 65 Jahre"),
            sample_flat(description="WBS mit besonderem Wohnbedarf für Ältere erforderlich"),
            sample_flat(raw_text="altersgerecht saniert und betreutes wohnen möglich"),
            sample_flat(title="Wohnen 60+ barrierearm"),
        ]
        for f in senior_cases:
            self.assertTrue(is_senior_housing(f), f"Failed to detect senior housing in {f}")
            ok, reason = matches_profile(f, self.profile_secondary)
            self.assertFalse(ok)
            self.assertEqual(reason, "senior")

    def test_student_filtering(self):
        student_cases = [
            sample_flat(title="WG-Zimmer in Berlin"),
            sample_flat(title="Gemütliche Studentenwohnung"),
            sample_flat(description="Nur für Studierende und Auszubildende"),
            sample_flat(description="Studenten only"),
            sample_flat(raw_text="Immatrikulierte Azubi Wohnheim"),
            sample_flat(title="Möbliertes Zimmer für Studenten"),
        ]
        for f in student_cases:
            self.assertTrue(is_student_housing(f), f"Failed to detect student housing in {f}")
            ok, reason = matches_profile(f, self.profile_secondary)
            self.assertFalse(ok)
            self.assertEqual(reason, "student")

        # Allow student if explicitly set
        student_allowed_profile = Profile(
            id="student",
            display_name="Student",
            allow_student=True,
            warm_rent_max=700.0,
        )
        ok, reason = matches_profile(sample_flat(title="Studentenwohnung"), student_allowed_profile)
        self.assertTrue(ok)

    def test_wbs_filtering_strictness(self):
        # WBS 100 fits
        ok, _ = matches_profile(sample_flat(raw_text="WBS 100 erforderlich"), self.profile_secondary)
        self.assertTrue(ok)

        # WBS 220 rejected when profile only allows 100
        ok, reason = matches_profile(sample_flat(raw_text="nur mit WBS 220"), self.profile_secondary)
        self.assertFalse(ok)
        self.assertEqual(reason, "wbs")

        # WBS mit besonderem Wohnbedarf rejected when 220 not in allowed
        ok, reason = matches_profile(sample_flat(raw_text="WBS mit besonderem Wohnbedarf"), self.profile_secondary)
        self.assertFalse(ok)
        self.assertEqual(reason, "wbs")

        # WBS erforderlich: Nein or ohne WBS does not reject
        ok, _ = matches_profile(sample_flat(raw_text="WBS erforderlich: Nein"), self.profile_secondary)
        self.assertTrue(ok)
        ok, _ = matches_profile(sample_flat(raw_text="Schöne Wohnung ohne WBS zu mieten"), self.profile_secondary)
        self.assertTrue(ok)


class MockScraperParsingTests(unittest.TestCase):
    def test_degewo_parsing_logic(self):
        mock_api_item = {
            "id": 998877,
            "headline": "Gemütliche Wohnung in Marzahn",
            "number_of_rooms": "2,0",
            "living_space": "52,4 m²",
            "total_rent": "510,50 €",
            "cold_rent": "380,00 €",
            "full_address": "Musterweg 12, 12681 Berlin",
            "slug": "gemuetliche-wohnung-in-marzahn",
            "wbs_required": False,
        }
        rooms = flat_monitor.parse_german_float(mock_api_item["number_of_rooms"])
        area = flat_monitor.parse_german_float(mock_api_item["living_space"])
        warm = flat_monitor.parse_german_float(mock_api_item["total_rent"])
        cold = flat_monitor.parse_german_float(mock_api_item["cold_rent"])
        self.assertEqual(rooms, 2.0)
        self.assertEqual(area, 52.4)
        self.assertEqual(warm, 510.5)
        self.assertEqual(cold, 380.0)

    def test_howoge_parsing_logic(self):
        mock_obj = {
            "uid": "12345",
            "title": "HOWOGE Mietwohnung",
            "rooms": "1,5",
            "area": "44,1",
            "rent": "490,00",
            "district": "Lichtenberg",
            "wbs": "WBS erforderlich: ja",
        }
        rooms = flat_monitor.parse_german_float(mock_obj["rooms"])
        warm = flat_monitor.parse_german_float(mock_obj["rent"])
        wbs_raw = str(mock_obj["wbs"]).lower()
        wbs_req = True if "ja" in wbs_raw else False
        self.assertEqual(rooms, 1.5)
        self.assertEqual(warm, 490.0)
        self.assertTrue(wbs_req)


class WbmAutoApplyTests(unittest.TestCase):
    def setUp(self):
        self.sample_html = """
        <div class="tx-powermail">
            <form class="powermail_form powermail_form_2" action="/wohnungen-berlin/angebote/details/?tx_powermail_pi1%5Baction%5D=create&amp;tx_powermail_pi1%5Bcontroller%5D=Form#c722" method="post">
                <input type="hidden" name="tx_powermail_pi1[__referrer][@extension]" value="Powermail">
                <input type="hidden" name="tx_powermail_pi1[__referrer][@controller]" value="Form">
                <input type="hidden" name="tx_powermail_pi1[__trustedProperties]" value="trusted_hash_123">
                <input type="hidden" name="tx_powermail_pi1[field][objekt]" value="1-7511/8/60">
                <input type="hidden" name="tx_powermail_pi1[mail][form]" value="2">
                <input type="text" name="tx_powermail_pi1[field][__hp]" value="">
            </form>
        </div>
        """

    def test_wbm_form_extraction(self):
        extracted = wbm_applier.extract_powermail_form(self.sample_html, "https://www.wbm.de/angebote/details/sample/")
        self.assertIsNotNone(extracted)
        self.assertIn("action", extracted)
        self.assertIn("fields", extracted)
        fields = extracted["fields"]
        self.assertEqual(fields.get("tx_powermail_pi1[__trustedProperties]"), "trusted_hash_123")
        self.assertEqual(fields.get("tx_powermail_pi1[field][objekt]"), "1-7511/8/60")

    def test_wbm_form_preparation(self):
        extracted = wbm_applier.extract_powermail_form(self.sample_html, "https://www.wbm.de/angebote/details/sample/")
        applicant = {
            "anrede": "Herr",
            "vorname": "Max",
            "name": "Mustermann",
            "strasse": "Hauptstr. 10",
            "plz": "10115",
            "ort": "Berlin",
            "email": "max@example.com",
            "telefon": "+49151000000",
            "has_wbs": True,
            "wbs_income_level": "100",
            "wbs_rooms": "2",
        }
        prepared = wbm_applier.prepare_wbm_form_data(extracted, applicant)
        self.assertEqual(prepared["tx_powermail_pi1[field][vorname]"], "Max")
        self.assertEqual(prepared["tx_powermail_pi1[field][name]"], "Mustermann")
        self.assertEqual(prepared["tx_powermail_pi1[field][e_mail]"], "max@example.com")
        self.assertEqual(prepared["tx_powermail_pi1[field][wbsvorhanden]"], "1")
        self.assertEqual(prepared["tx_powermail_pi1[field][einkommensgrenzenacheinkommensbescheinigung9]"], "100")
        self.assertEqual(prepared["tx_powermail_pi1[field][datenschutzhinweis][]"], "1")
        self.assertEqual(prepared["tx_powermail_pi1[field][__hp]"], "")


class SQLiteSingleConnectionTests(unittest.TestCase):
    def test_shared_connection_seen_checks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "flats_test.db")
            conn = flat_monitor.init_db(db_path)
            f = sample_flat()
            self.assertFalse(monitor_dispatch.is_profile_seen(conn, "secondary", f))
            monitor_dispatch.mark_profile_seen(conn, "secondary", f)
            self.assertTrue(monitor_dispatch.is_profile_seen(conn, "secondary", f))
            conn.close()


if __name__ == "__main__":
    unittest.main()
