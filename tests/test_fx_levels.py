"""Pip/spread-aware FX levels."""
import unittest

from v2 import levels


class TestFXLevels(unittest.TestCase):
    def test_long_spread_worsens_entry_and_rr_is_post_spread(self):
        # EUR/USD-like: pip 0.0001, 0.6 pip spread. Zone 1.1000-1.1010, mid 1.1005.
        lv = levels.compute_levels_fx(
            "long", 1.1000, 1.1010, atr=0.0020, price=1.1005,
            symbol="EURUSD=X", pip_size=0.0001, spread_pips=0.6,
            equity=10_000, risk_pct=0.005, std_lot=100_000)
        self.assertIsNotNone(lv)
        # entry worsened up by 0.6 pip from the 1.1005 mid
        self.assertAlmostEqual(lv["entry"], 1.1005 + 0.6 * 0.0001, places=5)
        self.assertEqual(lv["rr"], 2.0)
        # TP1 is exactly 2R above entry; risk = entry - stop
        risk = lv["entry"] - lv["stop_loss"]
        self.assertAlmostEqual(lv["tp1"], lv["entry"] + 2 * risk, places=5)
        self.assertAlmostEqual(lv["tp2"], lv["entry"] + 3 * risk, places=5)
        self.assertGreater(lv["risk_pips"], 0)
        self.assertGreater(lv["lots"], 0)

    def test_jpy_pip_size_is_one_cent(self):
        lv = levels.compute_levels_fx(
            "short", 160.00, 160.20, atr=0.30, price=160.10,
            symbol="USDJPY=X", pip_size=0.01, spread_pips=1.0,
            equity=10_000, risk_pct=0.005, std_lot=100_000)
        self.assertIsNotNone(lv)
        # short entry worsened DOWN by spread
        self.assertAlmostEqual(lv["entry"], 160.10 - 1.0 * 0.01, places=5)
        self.assertLess(lv["tp1"], lv["entry"])  # targets below for a short

    def test_absurdly_wide_stop_is_rejected(self):
        # atr so large the stop exceeds MAX_RISK_PCT of price -> None
        lv = levels.compute_levels_fx(
            "long", 1.10, 1.11, atr=0.5, price=1.10,
            symbol="EURUSD=X", pip_size=0.0001, spread_pips=0.6,
            equity=10_000, risk_pct=0.005, std_lot=100_000)
        self.assertIsNone(lv)


if __name__ == "__main__":
    unittest.main()
