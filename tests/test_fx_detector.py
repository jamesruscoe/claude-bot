"""FX detector calibration: vol-scaled impulse, wider retest window,
close-to-close impulse, and per-stage rejection diagnostics."""
import unittest
from datetime import datetime, timezone

import smc_detector as smc
from market_data import Bar
from v2 import config as cfg


def _obretest_series(trailing_out_of_zone: int = 0):
    """Synthetic uptrend -> red OB candle -> 3-bar bullish impulse -> pullback
    into the OB zone (retest). `trailing_out_of_zone` appends N bars ABOVE the
    zone after the retest, so the first touch recedes into the past."""
    t0 = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    day = 86_400_000
    bars, price = [], 100.0
    for i in range(60):
        price += 0.05
        bars.append(Bar(t=t0 + i * day, o=price, h=price + 0.5, l=price - 0.5, c=price + 0.1, v=1e6))
    ob = bars[-1].c
    bars.append(Bar(t=t0 + 60 * day, o=ob + 0.2, h=ob + 0.3, l=ob - 0.8, c=ob - 0.6, v=1e6))  # red OB
    base = bars[-1].c
    for i in range(3):
        base += 1.6
        bars.append(Bar(t=t0 + (61 + i) * day, o=base - 1.5, h=base + 0.3, l=base - 1.6, c=base, v=2e6))
    # pullback into the OB zone (first retest). Small own-bar o->c so the
    # pullback itself isn't mistaken for a fresh (short) impulse.
    bars.append(Bar(t=t0 + 64 * day, o=ob - 0.3, h=ob + 0.1, l=ob - 0.7, c=ob - 0.2, v=1e6))
    top = base + 2
    for j in range(trailing_out_of_zone):  # bars that stay above the zone
        bars.append(Bar(t=t0 + (65 + j) * day, o=top, h=top + 0.3, l=top - 0.2, c=top + 0.1, v=1e6))
    return bars


def _bos_series():
    """Swing high at idx4 (level 101.20), broken at idx8, first retest touch at
    idx9, then two bars hovering just above the level. Clean for a window test
    (BOS has no impulse-direction ambiguity)."""
    t0 = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    day = 86_400_000
    ohlc = [
        (100.0, 100.30, 99.80, 100.10), (100.1, 100.40, 99.90, 100.20),
        (100.2, 100.50, 100.00, 100.30), (100.3, 100.70, 100.10, 100.50),
        (100.5, 101.20, 100.30, 101.00),   # 4: swing high, h=101.20
        (101.0, 101.05, 100.60, 100.70), (100.7, 100.80, 100.40, 100.60),
        (100.6, 100.90, 100.30, 100.80),
        (100.8, 101.60, 100.70, 101.50),   # 8: break (close 101.50 > 101.20)
        (101.5, 101.55, 101.15, 101.30),   # 9: first retest (low 101.15 <= 101.20)
        (101.3, 101.50, 101.25, 101.40),   # 10: hover above
        (101.4, 101.60, 101.30, 101.50),   # 11: hover above
    ]
    return [Bar(t=t0 + i * day, o=o, h=h, l=l, c=c, v=1e6)
            for i, (o, h, l, c) in enumerate(ohlc)]


class TestRetestWindow(unittest.TestCase):
    def test_window1_fires_when_current_bar_is_the_retest(self):
        bars = _bos_series()[:10]  # current bar (idx9) is the first retest
        self.assertIsNotNone(smc.detect_bos_retest(bars, retest_window=1))

    def test_window1_misses_a_2bar_old_retest_but_window3_catches_it(self):
        bars = _bos_series()  # current bar is idx11; first retest was idx9 (2 bars ago)
        self.assertIsNone(smc.detect_bos_retest(bars, retest_window=1))
        self.assertIsNotNone(smc.detect_bos_retest(bars, retest_window=3))


class TestVolScaledImpulse(unittest.TestCase):
    def test_threshold_scales_with_atr(self):
        self.assertAlmostEqual(cfg.fx_impulse_threshold(0.0057, 1.14),
                               cfg.FX_IMPULSE_ATR_MULT * 0.0057 / 1.14, places=6)

    def test_falls_back_without_atr(self):
        self.assertEqual(cfg.fx_impulse_threshold(None, 1.14), cfg.FX_OB_IMPULSE_THRESHOLD)
        self.assertEqual(cfg.fx_impulse_threshold(0.005, 0), cfg.FX_OB_IMPULSE_THRESHOLD)


class TestC2CImpulse(unittest.TestCase):
    def test_c2c_flag_is_plumbed_and_detects_a_clear_impulse(self):
        # c2c is a correctness tidy, not a behavioural lever: on real multi-bar
        # data it agrees with open-to-close (2-3 bar o2c windows already anchor on
        # ~prior close). Just verify the flag threads through and detects.
        bars = _obretest_series(trailing_out_of_zone=0)
        self.assertIsNotNone(
            smc.detect_ob_retest(bars, impulse_threshold=0.01, impulse_c2c=True, retest_window=3))


class TestStageDiagnostics(unittest.TestCase):
    def test_flat_series_records_stages(self):
        bars = [Bar(t=i * 86_400_000, o=100, h=100.1, l=99.9, c=100, v=1) for i in range(40)]
        _, _, sig = smc.score_setups(bars, impulse_threshold=0.03)
        self.assertEqual(sig["ob_stage"], "no_impulse")
        self.assertEqual(sig["bos_stage"], "no_swings")


if __name__ == "__main__":
    unittest.main()
