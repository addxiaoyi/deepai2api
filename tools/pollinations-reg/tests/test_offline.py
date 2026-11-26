"""离线测试：站点描述自洽 + Key 解析（不联网）。"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[2] / "tools" / "linekit"))

from linekit.session import find_keys  # noqa: E402
from pollinations_reg.site import SITE  # noqa: E402


def test_site_points_at_https():
    assert SITE.base_url.startswith("https://")
    assert SITE.api_base.startswith("https://")


def test_key_candidates_are_pairs():
    for path, method in SITE.key_candidates:
        assert path.startswith("/"), path
        assert method in ("GET", "POST"), method


def test_key_prefix_matches_extractor():
    assert SITE.key_prefix == ""  # 匿名线没有 Key


def test_models_registered():
    assert SITE.models, "至少要登记一个模型"


def test_describe_is_json_safe():
    import json

    payload = SITE.describe()
    assert json.loads(json.dumps(payload))["id"] == SITE.id
