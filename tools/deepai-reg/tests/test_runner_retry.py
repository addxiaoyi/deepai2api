"""Retry-round behaviour: a failed unit is re-attempted once and the reason kept."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepai_reg.config import Config  # noqa: E402
from deepai_reg.errors import SignupError  # noqa: E402
from deepai_reg.runner import Runner  # noqa: E402
from deepai_reg.store import Store  # noqa: E402


class FlakyRunner(Runner):
    """Fails unit 0 on the first round, succeeds on the retry round."""

    def __init__(self, cfg, store, **kwargs):
        super().__init__(cfg, store, **kwargs)
        self.calls: list[tuple[int, int]] = []

    def signup(self, index: int, round_no: int = 0) -> dict:
        self.calls.append((index, round_no))
        if index == 0 and round_no == 0:
            self.store.record_failure(
                email="broken@example.com",
                domain="example.com",
                engine="protocol",
                proxy="",
                kind="email_rejected",
                reason="Please use your own email address.",
                round_no=round_no,
            )
            self.emit({"type": "failed", "index": 1, "kind": "email_rejected", "reason": "rejected"})
            raise SignupError("Please use your own email address.")
        account = {
            "email": f"ok{index}{round_no}@example.com",
            "password": "pw!A1234567",
            "api_key": f"0000-0000-0000-000{index}{round_no}",
            "username": f"user{index}",
            "is_pro": False,
            "plan": "free",
        }
        self.store.add(account, engine="protocol", proxy="")
        return account


class TestRetryRounds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config.load(
            None,
            {
                "count": 2,
                "engine": "protocol",
                "concurrency": 1,
                "attempts": 1,
                "retry_rounds": 1,
                "delay": (0, 0),
                "output_dir": root / "out",
                "db_path": root / "out" / "test.sqlite3",
            },
        )
        self.store = Store(self.cfg.db_path)
        self.runners: list[Runner] = []

    def tearDown(self):
        for runner in self.runners:
            runner.close()
        self.store.close()
        self.tmp.cleanup()

    def test_failed_unit_is_retried_and_reason_is_kept(self):
        events: list[dict] = []
        runner = FlakyRunner(self.cfg, self.store, on_event=events.append, quiet=True)
        self.runners.append(runner)
        summary = runner.run(2)

        self.assertEqual(summary["created"], 2)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["retries"], 1)
        self.assertIn((0, 0), runner.calls)
        self.assertIn((0, 1), runner.calls)  # the retry round revisited it
        kinds = {row["kind"]: row["count"] for row in summary["failure_reasons"]}
        self.assertEqual(kinds.get("email_rejected"), 1)
        self.assertTrue(any(e.get("type") == "retry" for e in events))

    def test_retry_rounds_zero_leaves_failure_recorded(self):
        self.cfg.retry_rounds = 0
        events: list[dict] = []
        runner = FlakyRunner(self.cfg, self.store, on_event=events.append, quiet=True)
        self.runners.append(runner)
        summary = runner.run(2)
        self.assertEqual(summary["created"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["retries"], 0)
        self.assertEqual(self.store.failure_summary()[0]["kind"], "email_rejected")


if __name__ == "__main__":
    unittest.main()
