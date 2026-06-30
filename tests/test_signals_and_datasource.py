"""Rejection reasons from build_candidate + data-source factory/parsing.
No network is touched."""
import unittest

from market_data import Bar
from v2 import config as cfg
from v2 import datasource, signals


class TestRejectionReasons(unittest.TestCase):
    def test_too_few_bars(self):
        cand, reason = signals.build_candidate("X", [], live_price=1.0)
        self.assertIsNone(cand)
        self.assertEqual(reason, "too_few_bars")

    def test_flat_series_yields_no_setup(self):
        # 40 identical bars: no impulse, no swing -> score 0 -> "no_setup"
        bars = [Bar(t=i * 86_400_000, o=100, h=100.1, l=99.9, c=100, v=1) for i in range(40)]
        cand, reason = signals.build_candidate("X", bars, live_price=100.0)
        self.assertIsNone(cand)
        self.assertIn(reason, {"no_setup", "regime_blocked"})


class TestDataSourceFactory(unittest.TestCase):
    def setUp(self):
        self._orig = cfg.FX_ENABLED

    def tearDown(self):
        cfg.FX_ENABLED = self._orig

    def test_factory_defaults_to_equities(self):
        cfg.FX_ENABLED = False
        src = datasource.get_data_source()
        self.assertEqual(src.name, "equities")
        self.assertFalse(src.intraday_supported)

    def test_factory_returns_fx_when_enabled(self):
        cfg.FX_ENABLED = True
        src = datasource.get_data_source()
        self.assertEqual(src.name, "fx")
        self.assertTrue(src.intraday_supported)
        self.assertEqual(src.pip_size("USDJPY=X"), 0.01)
        self.assertEqual(src.pip_size("EURUSD=X"), 0.0001)


class TestDfToBars(unittest.TestCase):
    def test_drops_malformed_and_nonpositive(self):
        class FakeIdx:
            def __init__(self, ts): self._ts = ts
            def timestamp(self): return self._ts

        rows = [
            (FakeIdx(1_700_000_000), {"Open": 1.1, "High": 1.2, "Low": 1.0, "Close": 1.15, "Volume": 0}),
            (FakeIdx(1_700_086_400), {"Open": 0, "High": 0, "Low": 0, "Close": 0, "Volume": 0}),  # dropped
        ]

        class FakeDF:
            empty = False
            def iterrows(self): return iter(rows)

        bars = datasource._df_to_bars(FakeDF())
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].c, 1.15)


if __name__ == "__main__":
    unittest.main()
