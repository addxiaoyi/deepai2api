"""Config/identity/plugin-surface tests — pure functions, no network."""

from __future__ import annotations

import random
import string
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepai_reg.browser import _proxy_dict  # noqa: E402
from deepai_reg.config import Config, normalize_proxy  # noqa: E402
from deepai_reg.identity import make_persona  # noqa: E402
from deepai_reg.net import firefox_user_agent  # noqa: E402


class TestProxyNormalisation(unittest.TestCase):
    def test_bare_host_gets_scheme(self):
        self.assertEqual(normalize_proxy("10.0.0.8:8080"), "http://10.0.0.8:8080")

    def test_scheme_is_kept(self):
        self.assertEqual(normalize_proxy("socks5://h:1"), "socks5://h:1")

    def test_camoufox_proxy_dict_splits_credentials(self):
        self.assertEqual(
            _proxy_dict("http://u:p@1.2.3.4:8080"),
            {"server": "http://1.2.3.4:8080", "username": "u", "password": "p"},
        )
        self.assertEqual(_proxy_dict("http://1.2.3.4:8080"), {"server": "http://1.2.3.4:8080"})
        self.assertIsNone(_proxy_dict(""))


class TestConfigLoading(unittest.TestCase):
    def test_yaml_and_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yaml"
            path.write_text(
                "engine: browser\ncount: 7\nconcurrency: 3\ndelay: [1, 2]\n"
                "proxies: ['1.1.1.1:8080', 'socks5://2.2.2.2:1080']\n"
                "mail:\n  provider: mailtm\n  domain: example.org\n",
                encoding="utf-8",
            )
            cfg = Config.load(path, {"count": 2, "engine": "protocol"})
            self.assertEqual(cfg.engine, "protocol")
            self.assertEqual(cfg.count, 2)
            self.assertEqual(cfg.delay, (1.0, 2.0))
            self.assertEqual(cfg.proxies[0], "http://1.1.1.1:8080")
            self.assertEqual(cfg.proxy_for(0), "http://1.1.1.1:8080")
            self.assertEqual(cfg.proxy_for(3), "socks5://2.2.2.2:1080")
            self.assertEqual(cfg.mail.provider, "mailtm")
            self.assertEqual(cfg.mail.domain, "example.org")

    def test_fixed_address_override_switches_provider(self):
        cfg = Config.load(None, {"mail_provider": "fixed", "mail_address": "me+1@example.org"})
        self.assertEqual(cfg.mail.provider, "fixed")
        self.assertEqual(cfg.mail.address, "me+1@example.org")


class TestPersona(unittest.TestCase):
    def test_password_policy_shape(self):
        persona = make_persona(random.Random(7))
        self.assertGreaterEqual(len(persona.password), 12)
        self.assertTrue(any(c.isupper() for c in persona.password))
        self.assertTrue(any(c.islower() for c in persona.password))
        self.assertTrue(any(c.isdigit() for c in persona.password))
        self.assertTrue(any(c in string.punctuation for c in persona.password))
        self.assertRegex(persona.local_part, r"^[a-z]+\.[a-z]+\d+$")

    def test_seeded_persona_is_reproducible(self):
        self.assertEqual(make_persona(random.Random(3)), make_persona(random.Random(3)))

    def test_user_agent_looks_like_firefox(self):
        ua = firefox_user_agent(random.Random(1))
        self.assertIn("Firefox/", ua)
        self.assertIn("Gecko/20100101", ua)


if __name__ == "__main__":
    unittest.main()
