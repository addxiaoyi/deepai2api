"""Domain scorecard tests — pure SQLite, no network."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepai_reg.mail_pool import DomainPool, apex_domain  # noqa: E402


class TestApexDomain(unittest.TestCase):
    def test_three_label_subdomain_collapses(self):
        self.assertEqual(apex_domain("abc123@dsq.repairhubde.com"), "repairhubde.com")

    def test_plain_domain_is_untouched(self):
        self.assertEqual(apex_domain("a@example.com"), "example.com")

    def test_two_label_suffix_is_kept(self):
        self.assertEqual(apex_domain("a@mail.example.co.uk"), "example.co.uk")

    def test_garbage_returns_empty(self):
        self.assertEqual(apex_domain("not-an-address"), "")


class TestDomainPool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pool = DomainPool(Path(self.tmp.name) / "test.sqlite3")

    def tearDown(self):
        self.pool.close()
        self.tmp.cleanup()

    def test_learned_good_domain_comes_first(self):
        self.pool.record_ok("tempmail_lol", "good.example")
        order = self.pool.candidates("tempmail_lol", ["other.example", "good.example"])
        self.assertEqual(order[0], "good.example")

    def test_refused_domain_is_parked(self):
        self.pool.record_bad("tempmail_lol", "blocked.example", "Please use your own email address.")
        order = self.pool.candidates("tempmail_lol", ["blocked.example", "fresh.example"])
        self.assertEqual(order, ["fresh.example"])

    def test_everything_parked_falls_back_to_configured(self):
        self.pool.record_bad("tempmail_lol", "only.example", "nope")
        self.assertEqual(self.pool.candidates("tempmail_lol", ["only.example"]), ["only.example"])

    def test_stats_are_provider_scoped(self):
        self.pool.record_ok("tempmail_lol", "a.example")
        self.pool.record_ok("mailtm", "b.example")
        rows = self.pool.rows("mailtm")
        self.assertEqual([r["domain"] for r in rows], ["b.example"])
        self.assertEqual(rows[0]["ok_count"], 1)


if __name__ == "__main__":
    unittest.main()
