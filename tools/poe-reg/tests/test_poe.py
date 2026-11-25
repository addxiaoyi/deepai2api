"""Poe factory tests: pool lifecycle, config, template shape (no network)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poe_reg.config import Config  # noqa: E402
from poe_reg.pool import Pool  # noqa: E402
from poe_reg import gql_templates as tpl  # noqa: E402


class TestPoePool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pool = Pool(Path(self.tmp.name) / "poe.sqlite3")

    def tearDown(self):
        self.pool.close()
        self.tmp.cleanup()

    def test_upsert_then_update_keeps_one_row(self):
        created = self.pool.upsert(
            {"email": "a@example.com", "status": "email_verified", "engine": "protocol",
             "cookies": {"p-b": "x"}}
        )
        self.assertTrue(created)
        again = self.pool.upsert(
            {"email": "a@example.com", "status": "active", "phone": "+14155550123", "engine": "browser"}
        )
        self.assertFalse(again)
        self.assertEqual(self.pool.count(), 1)
        self.assertEqual(self.pool.counts_by_status(), {"active": 1})
        self.assertEqual(self.pool.cookie_map("a@example.com"), {"p-b": "x"})

    def test_status_guard(self):
        self.pool.upsert({"email": "b@example.com", "status": "email_verified"})
        with self.assertRaises(ValueError):
            self.pool.mark("b@example.com", "nonsense")

    def test_export_jsonl(self):
        self.pool.upsert({"email": "c@example.com", "status": "phone_required", "cookies": {"p-b": "y"}})
        path = Path(self.tmp.name) / "poe.jsonl"
        self.assertEqual(self.pool.export_jsonl(path), 1)
        line = path.read_text(encoding="utf-8").strip()
        self.assertIn('"status": "phone_required"', line)
        self.assertIn('"p-b": "y"', line)


class TestPoeConfig(unittest.TestCase):
    def test_yaml_and_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yaml"
            path.write_text(
                "engine: browser\ncount: 3\ndelay: [5, 10]\nproxies: ['1.2.3.4:8080']\n"
                "sms:\n  provider: sms_activate\n  api_key: k\nmail:\n  provider: imap\n",
                encoding="utf-8",
            )
            cfg = Config.load(path, {"count": 1, "engine": "protocol"})
            self.assertEqual(cfg.engine, "protocol")
            self.assertEqual(cfg.count, 1)
            self.assertEqual(cfg.delay, (5.0, 10.0))
            self.assertEqual(cfg.proxies, ["http://1.2.3.4:8080"])
            self.assertTrue(cfg.sms.enabled)
            self.assertEqual(cfg.sms.provider, "sms_activate")
            self.assertEqual(cfg.mail["provider"], "imap")

    def test_sms_off_by_default(self):
        cfg = Config.load(None, {})
        self.assertFalse(cfg.sms.enabled)


class TestTemplates(unittest.TestCase):
    def test_build_keeps_hash_and_shape(self):
        body = tpl.build(tpl.SEND_CODE, emailAddress="x@y.z", recaptchaToken="t")
        self.assertEqual(body["queryName"], tpl.SEND_CODE)
        self.assertEqual(body["variables"]["emailAddress"], "x@y.z")
        self.assertIsNone(body["variables"]["phoneNumber"])
        self.assertEqual(len(body["extensions"]["hash"]), 64)

    def test_signup_template_has_subscriptions(self):
        body = tpl.build(tpl.SIGNUP_CODE, verificationCode="123456", emailAddress="a@b.c")
        self.assertEqual(body["variables"]["subscriptions"], [])

    def test_status_map_covers_the_phone_gate(self):
        self.assertEqual(tpl.SEND_CODE_STATUS["user_with_confirmed_email_not_found"], "signup")
        self.assertEqual(tpl.SEND_CODE_STATUS["automated_request_detected"], "automated")


if __name__ == "__main__":
    unittest.main()
