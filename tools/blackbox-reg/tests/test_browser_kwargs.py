"""指纹浏览器启动参数的离线用例（不需要真的拉起浏览器）。

守住两件最容易搞错的事：

1. **camoufox 是 Firefox**：绝不能把 Chrome UA / sec-ch-ua 注进去，否则开局就暴露；
2. **geoip 与 locale 不能同时给**：时区/语言必须由出口 IP 决定，两个都传会互相打架。

顺带守住 persistent_context（身份档复用）与 humanize 的语义映射。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blackbox_reg.browser import (  # noqa: E402
    BrowserUnavailable,
    available_stacks,
    camoufox_installed_version,
    camoufox_launch_kwargs,
    chromium_context_kwargs,
    detect_stack,
)
from blackbox_reg.identity import make_persona  # noqa: E402


class CamoufoxKwargsCase(unittest.TestCase):
    def test_geoip_wins_over_locale_and_no_chrome_ua(self):
        kwargs = camoufox_launch_kwargs(
            headless=True,
            humanize=1.0,
            proxy="http://user:pass@127.0.0.1:8080",
            geoip=True,
            locale="en-US",
            os_name="windows",
            disable_coop=True,
        )
        self.assertEqual(kwargs["proxy"], {"server": "http://user:pass@127.0.0.1:8080"})
        self.assertTrue(kwargs["geoip"])
        # geoip 开时不再传 locale
        self.assertNotIn("locale", kwargs)
        # 绝不注入 Chrome UA / sec-ch-ua
        self.assertNotIn("user_agent", kwargs)
        self.assertNotIn("userAgent", kwargs)
        # 不固定窗口尺寸
        self.assertNotIn("window", kwargs)
        self.assertTrue(kwargs["headless"])
        self.assertTrue(kwargs["disable_coop"])
        self.assertEqual(kwargs["os"], "windows")
        # humanize 语义：<=1 视为"开默认轨迹"
        self.assertIs(kwargs["humanize"], True)

    def test_locale_used_when_geoip_disabled(self):
        kwargs = camoufox_launch_kwargs(geoip=False, locale="ja-JP")
        self.assertEqual(kwargs["locale"], "ja-JP")
        self.assertNotIn("geoip", kwargs)

    def test_humanize_multiplier_maps_to_seconds_and_is_capped(self):
        self.assertEqual(camoufox_launch_kwargs(humanize=1.8, geoip=False)["humanize"], 1.8)
        self.assertEqual(camoufox_launch_kwargs(humanize=9.0, geoip=False)["humanize"], 3.0)

    def test_profile_dir_switches_to_persistent_context(self):
        kwargs = camoufox_launch_kwargs(profile_dir="/tmp/bbx-profile", geoip=False)
        self.assertTrue(kwargs["persistent_context"])
        self.assertEqual(kwargs["user_data_dir"], "/tmp/bbx-profile")

    def test_no_proxy_means_no_proxy_key(self):
        self.assertNotIn("proxy", camoufox_launch_kwargs(proxy="", geoip=False))


class ChromiumKwargsCase(unittest.TestCase):
    def test_chromium_context_is_self_consistent(self):
        persona = make_persona(seed="unit", country="US")
        kwargs = chromium_context_kwargs(persona)
        self.assertIn("Chrome/", kwargs["user_agent"])
        self.assertEqual(kwargs["locale"], persona.locale)
        self.assertEqual(kwargs["timezone_id"], persona.timezone)
        self.assertEqual(kwargs["extra_http_headers"]["Sec-CH-UA"], persona.sec_ch_ua)
        self.assertEqual(kwargs["viewport"]["width"], persona.viewport[0])
        self.assertNotIn("geoip", kwargs)


class StackCase(unittest.TestCase):
    def test_camoufox_detected_when_installed(self):
        stacks = available_stacks()
        if "camoufox" not in stacks:
            self.skipTest("本机没装 camoufox")
        self.assertEqual(detect_stack("camoufox"), "camoufox")
        self.assertEqual(detect_stack("auto"), stacks[0])

    def test_unknown_stack_raises_with_hint(self):
        with self.assertRaises(BrowserUnavailable) as ctx:
            detect_stack("playwright" if "playwright" not in available_stacks() else "camoufox-nope")
        self.assertIn("camoufox", str(ctx.exception))

    def test_installed_version_is_a_string(self):
        version = camoufox_installed_version()
        self.assertIsInstance(version, str)
        # 装了 pip 包但没 fetch 内核时就是空串，这里只要求不炸
        if version:
            self.assertRegex(version, r"^\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
