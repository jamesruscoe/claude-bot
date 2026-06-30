"""Phase 2 FX filters: session, correlation cap, news blackout (offline)."""
import unittest
from datetime import datetime, timezone

from v2 import config as cfg
from v2 import fx_filters, news_calendar


class TestSession(unittest.TestCase):
    def setUp(self):
        self._mode = cfg.FX_SESSION_MODE

    def tearDown(self):
        cfg.FX_SESSION_MODE = self._mode

    def test_off_allows_everything(self):
        cfg.FX_SESSION_MODE = "off"
        ok, _ = fx_filters.session_ok("EURUSD=X", datetime(2026, 1, 1, 2, tzinfo=timezone.utc))
        self.assertTrue(ok)

    def test_overlap_only(self):
        cfg.FX_SESSION_MODE = "overlap"
        inside = datetime(2026, 1, 1, 14, tzinfo=timezone.utc)
        outside = datetime(2026, 1, 1, 3, tzinfo=timezone.utc)
        self.assertTrue(fx_filters.session_ok("EURUSD=X", inside)[0])
        self.assertFalse(fx_filters.session_ok("EURUSD=X", outside)[0])

    def test_skip_asia_exempts_jpy(self):
        cfg.FX_SESSION_MODE = "skip_asia"
        asia = datetime(2026, 1, 1, 2, tzinfo=timezone.utc)  # 02:00 UTC = Asia
        self.assertFalse(fx_filters.session_ok("EURUSD=X", asia)[0])
        self.assertTrue(fx_filters.session_ok("USDJPY=X", asia)[0])


class TestCorrelationCap(unittest.TestCase):
    def setUp(self):
        self._cap = cfg.FX_MAX_PER_CCY
        cfg.FX_MAX_PER_CCY = 2

    def tearDown(self):
        cfg.FX_MAX_PER_CCY = self._cap

    def test_blocks_third_usd_short(self):
        # long EURUSD and long GBPUSD = two USD shorts already at the cap
        book = [{"symbol": "EURUSD=X", "direction": "long"},
                {"symbol": "GBPUSD=X", "direction": "long"}]
        ok, why = fx_filters.correlation_cap_ok("AUDUSD=X", "long", book)
        self.assertFalse(ok)
        self.assertIn("USD", why)

    def test_allows_offsetting_exposure(self):
        book = [{"symbol": "EURUSD=X", "direction": "long"}]  # +EUR -USD
        # buying USDJPY is +USD, which offsets toward zero — allowed
        ok, _ = fx_filters.correlation_cap_ok("USDJPY=X", "long", book)
        self.assertTrue(ok)


class TestNewsBlackout(unittest.TestCase):
    def setUp(self):
        self._on = cfg.FX_NEWS_FILTER
        cfg.FX_NEWS_FILTER = True

    def tearDown(self):
        cfg.FX_NEWS_FILTER = self._on

    def test_parse_keeps_only_high_impact(self):
        raw = [
            {"country": "USD", "impact": "High", "date": "2026-01-01T13:30:00Z"},
            {"country": "EUR", "impact": "Low", "date": "2026-01-01T09:00:00Z"},
        ]
        evs = news_calendar._parse_events(raw)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["currency"], "USD")

    def test_blocks_within_window(self):
        events = [{"currency": "USD", "impact": "high",
                   "dt": datetime(2026, 1, 1, 13, 30, tzinfo=timezone.utc)}]
        now = datetime(2026, 1, 1, 13, 15, tzinfo=timezone.utc)  # 15 min before
        blocked, _ = news_calendar.news_blackout("EURUSD=X", now, events=events)
        self.assertTrue(blocked)

    def test_allows_far_from_event(self):
        events = [{"currency": "USD", "impact": "high",
                   "dt": datetime(2026, 1, 1, 13, 30, tzinfo=timezone.utc)}]
        now = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
        blocked, _ = news_calendar.news_blackout("EURUSD=X", now, events=events)
        self.assertFalse(blocked)

    def test_fails_open_when_feed_unreachable(self):
        import tempfile
        from pathlib import Path
        orig_url, orig_dir = cfg.FF_CALENDAR_URL, cfg.STATE_DIR
        cfg.FF_CALENDAR_URL = "http://127.0.0.1:9/never"   # connection refused fast
        cfg.STATE_DIR = Path(tempfile.mkdtemp(prefix="botv2_news_"))  # no cache file
        try:
            blocked, why = news_calendar.news_blackout(
                "EURUSD=X", datetime(2026, 1, 1, tzinfo=timezone.utc), events=None)
            self.assertFalse(blocked)            # fail OPEN — never block on outage
            self.assertIn("failed open", why)
        finally:
            cfg.FF_CALENDAR_URL, cfg.STATE_DIR = orig_url, orig_dir


if __name__ == "__main__":
    unittest.main()
