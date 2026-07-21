"""Range-breakout detector geometry — pure synthetic bars, no data, no network.
Verifies the PRE-REGISTERED definition in v2/patterns.py behaves as specified."""
import unittest

from market_data import Bar
from v2 import patterns

_ATR = 0.01              # tol=0.005, width-max=0.04, breakout-buffer=0.0025
_BASE_T = 1_600_000_000_000
_DAY = 86_400_000

PEAKS = {4, 10, 16, 22}      # swing highs at the ceiling R=1.100
TROUGHS = {7, 13, 19, 25}    # swing lows at the floor S=1.080


def _range_bars(n=30, *, floor=1.080, peaks=PEAKS, troughs=TROUGHS):
    """A clean horizontal range oscillating between S=floor and R=1.100."""
    bars = []
    for i in range(n):
        h, l, c = 1.093, 1.087, 1.090
        if i in peaks:
            h, c = 1.100, 1.098
        if i in troughs:
            l, c = floor, floor + 0.002
        bars.append(Bar(t=_BASE_T + i * _DAY, o=c, h=h, l=l, c=c, v=0))
    return bars


def _set_last(bars, c, h=None, l=None):
    b = bars[-1]
    bars[-1] = Bar(t=b.t, o=b.o, h=h if h is not None else max(b.h, c),
                   l=l if l is not None else min(b.l, c), c=c, v=0)
    return bars


class TestRangeBreakout(unittest.TestCase):
    def test_long_breakout_fires(self):
        bars = _set_last(_range_bars(), c=1.106, h=1.107, l=1.089)
        s = patterns.detect_range_breakout(bars, atr=_ATR)
        self.assertIsNotNone(s)
        self.assertEqual(s.direction, "long")
        self.assertAlmostEqual(s.key_levels["resistance"], 1.100, places=3)
        self.assertAlmostEqual(s.zone_low, 1.106, places=3)  # entry = breakout close

    def test_short_breakout_fires(self):
        bars = _set_last(_range_bars(), c=1.074, h=1.091, l=1.073)
        s = patterns.detect_range_breakout(bars, atr=_ATR)
        self.assertIsNotNone(s)
        self.assertEqual(s.direction, "short")
        self.assertAlmostEqual(s.key_levels["support"], 1.080, places=3)

    def test_no_breakout_when_close_inside(self):
        bars = _range_bars()  # last close 1.090, well inside
        self.assertIsNone(patterns.detect_range_breakout(bars, atr=_ATR))

    def test_marginal_poke_below_buffer_does_not_fire(self):
        # close just above R but < R + 0.25*ATR (1.1025)
        bars = _set_last(_range_bars(), c=1.1010, h=1.1015, l=1.090)
        self.assertIsNone(patterns.detect_range_breakout(bars, atr=_ATR))

    def test_too_wide_is_rejected(self):
        # floor at 1.00 -> width 0.10 > 4*ATR(0.04): a trend, not a range
        bars = _set_last(_range_bars(floor=1.000), c=1.106, h=1.107, l=1.089)
        self.assertIsNone(patterns.detect_range_breakout(bars, atr=_ATR))

    def test_too_few_touches_rejected(self):
        bars = _set_last(_range_bars(peaks={4}), c=1.106, h=1.107, l=1.089)
        self.assertIsNone(patterns.detect_range_breakout(bars, atr=_ATR))

    def test_sustained_breakout_does_not_refire(self):
        # prior close already beyond R -> not a fresh breakout bar
        bars = _range_bars()
        bars[-2] = Bar(t=bars[-2].t, o=1.104, h=1.106, l=1.103, c=1.105, v=0)
        bars = _set_last(bars, c=1.108, h=1.109, l=1.104)
        self.assertIsNone(patterns.detect_range_breakout(bars, atr=_ATR))

    def test_no_atr_returns_none(self):
        bars = _set_last(_range_bars(), c=1.106)
        self.assertIsNone(patterns.detect_range_breakout(bars, atr=None))


if __name__ == "__main__":
    unittest.main()
