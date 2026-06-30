"""Phase 5 daily report rendering (offline)."""
import tempfile
import unittest
from pathlib import Path

from v2 import config as cfg
from v2 import report, store


class TestDailyReport(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="botv2_report_"))
        cfg.STATE_DIR = tmp
        cfg.DB_PATH = tmp / "ledger.db"
        cfg.CACHE_DIR = tmp / "cache"
        store.init_db()

    def test_renders_candidates_rejections_and_paper_note(self):
        payload = {
            "market": "fx", "skipped": False, "llm": False,
            "results": [
                {"symbol": "EURUSD=X", "candidate": True, "direction": "long",
                 "score": 100, "rr": 2.0, "take": True, "confidence": "medium",
                 "size": "half", "opened": True},
                {"symbol": "GBPUSD=X", "candidate": True, "direction": "short",
                 "score": 50, "rr": 2.0, "take": True, "confidence": "low",
                 "size": "quarter", "fx_blocked": "below calibrated FX_MIN_SCORE (50<100)"},
            ],
            "opened": [{"symbol": "EURUSD=X", "direction": "long", "entry_price": 1.1,
                        "stop_loss": 1.09, "tp1": 1.12, "tp2": 1.13, "size": "half"}],
            "closed": [],
        }
        text = report.build_report(payload)
        self.assertIn("Daily report", text)
        self.assertIn("EURUSD=X", text)
        self.assertIn("← opened", text)
        self.assertIn("blocked:", text)
        self.assertIn("paper only", text.lower())
        self.assertIn("OANDA", text)

    def test_handles_skipped_scan(self):
        text = report.build_report({"skipped": True, "reason": "weekend"})
        self.assertIn("skipped", text.lower())


if __name__ == "__main__":
    unittest.main()
