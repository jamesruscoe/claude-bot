"""Size is applied to recorded R (audit HIGH bug — sizing was cosmetic), plus
the rejection ledger round-trips."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from market_data import Bar
from v2 import config as cfg
from v2 import store


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="botv2_test_"))
        cfg.STATE_DIR = self.tmp
        cfg.DB_PATH = self.tmp / "ledger.db"
        cfg.JOURNAL_DIR = self.tmp / "journal"
        cfg.LESSONS_DIR = self.tmp / "lessons"
        cfg.CACHE_DIR = self.tmp / "cache"
        store.init_db()

    def _candidate(self):
        return {"symbol": "TEST", "direction": "long", "setups": ["ob_retest"],
                "regime": "bullish", "stop_loss": 99.0, "tp1": 102.0, "tp2": 103.0,
                "lots": 0.1}

    def _win_bar(self):
        t0 = int(datetime(2026, 1, 3, tzinfo=timezone.utc).timestamp() * 1000)
        return Bar(t=t0, o=100.0, h=103.5, l=100.0, c=103.2, v=0)


class TestSizedR(StoreTestBase):
    def test_half_size_halves_recorded_r(self):
        store.open_trade(None, self._candidate(), 100.0,
                         opened_at="2026-01-01T00:00:00+00:00", size="half")
        closed = store.resolve_open_trades({"TEST": [self._win_bar()]},
                                           now=datetime(2026, 1, 4, tzinfo=timezone.utc))
        self.assertEqual(len(closed), 1)
        # raw R = (103 - 100) / (100 - 99) = 3.0 ; sized at half = 1.5
        self.assertEqual(closed[0]["raw_r"], 3.0)
        self.assertEqual(closed[0]["pnl_r"], 1.5)

    def test_full_size_is_raw(self):
        store.open_trade(None, self._candidate(), 100.0,
                         opened_at="2026-01-01T00:00:00+00:00", size="full")
        closed = store.resolve_open_trades({"TEST": [self._win_bar()]},
                                           now=datetime(2026, 1, 4, tzinfo=timezone.utc))
        self.assertEqual(closed[0]["pnl_r"], closed[0]["raw_r"])


class TestRejectionLedger(StoreTestBase):
    def test_rejections_round_trip(self):
        store.record_rejection("2026-01-01T00:00:00+00:00", "EURUSD=X", "detector", "no_setup")
        store.record_rejection("2026-01-01T00:00:00+00:00", "GBPUSD=X", "detector", "no_setup")
        store.record_rejection("2026-01-01T00:00:00+00:00", "USDJPY=X", "detector", "regime_blocked")
        counts = store.rejection_counts()
        self.assertEqual(counts["no_setup"], 2)
        self.assertEqual(counts["regime_blocked"], 1)


if __name__ == "__main__":
    unittest.main()
