"""Honest intrabar resolution (audit master-bug fix)."""
import unittest
from datetime import datetime, timedelta, timezone

from market_data import Bar
from v2 import store


def _bar(day_offset: int, *, hi: float, lo: float, close: float) -> Bar:
    t0 = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    return Bar(t=t0 + day_offset * 86_400_000, o=close, h=hi, l=lo, c=close, v=0)


def _long(**kw):
    base = {"direction": "long", "entry_price": 100.0, "stop_loss": 99.0,
            "original_sl": 99.0, "tp1": 102.0, "tp2": 103.0, "tp1_hit": 0,
            "tp1_hit_at": None, "opened_at": "2026-01-01T00:00:00+00:00"}
    base.update(kw)
    return base


class TestWalkTrade(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 5, tzinfo=timezone.utc)

    def test_tp2_high_pierce_is_a_win(self):
        # A bar whose HIGH pierces TP2 wins — even though its close is back inside
        # (this is precisely what close-only resolution silently expired at ~0R).
        t = _long()
        bar = _bar(2, hi=103.5, lo=101.0, close=101.2)
        outcome, price = store.walk_trade(t, [bar], self.now)
        self.assertEqual(outcome, store.OUTCOME_WIN_TP2)
        self.assertEqual(price, 103.0)

    def test_stop_low_pierce_is_a_loss(self):
        t = _long()
        bar = _bar(1, hi=100.5, lo=98.5, close=99.2)
        outcome, price = store.walk_trade(t, [bar], self.now)
        self.assertEqual(outcome, store.OUTCOME_LOSS)
        self.assertEqual(price, 99.0)

    def test_sl_first_tie_break_when_bar_spans_both(self):
        # One bar that touches BOTH stop and TP2 -> conservative LOSS.
        t = _long()
        bar = _bar(1, hi=104.0, lo=98.0, close=101.0)
        outcome, _ = store.walk_trade(t, [bar], self.now)
        self.assertEqual(outcome, store.OUTCOME_LOSS)

    def test_breakeven_after_tp1_then_reversal(self):
        t = _long()
        b1 = _bar(1, hi=102.2, lo=100.5, close=101.8)   # hits TP1 -> trail to entry
        b2 = _bar(2, hi=101.0, lo=99.5, close=99.8)      # dips to entry (trailed stop)
        outcome, price = store.walk_trade(t, [b1, b2], self.now)
        self.assertEqual(outcome, store.OUTCOME_BREAKEVEN)
        self.assertEqual(price, 100.0)
        self.assertEqual(t["tp1_hit"], 1)

    def test_expiry_when_window_elapses_without_a_hit(self):
        t = _long()
        bars = [_bar(i, hi=101.0, lo=99.5, close=100.2) for i in range(1, 13)]
        late = datetime(2026, 1, 30, tzinfo=timezone.utc)
        outcome, _ = store.walk_trade(t, bars, late)
        self.assertEqual(outcome, store.OUTCOME_EXPIRED)

    def test_stays_open_within_window(self):
        t = _long()
        bars = [_bar(1, hi=101.0, lo=99.5, close=100.2)]
        outcome, _ = store.walk_trade(t, bars, datetime(2026, 1, 2, tzinfo=timezone.utc))
        self.assertIsNone(outcome)

    def test_short_win(self):
        t = _long(direction="short", entry_price=100.0, stop_loss=101.0,
                  original_sl=101.0, tp1=98.0, tp2=97.0)
        bar = _bar(1, hi=99.0, lo=96.5, close=98.5)
        outcome, price = store.walk_trade(t, [bar], self.now)
        self.assertEqual(outcome, store.OUTCOME_WIN_TP2)
        self.assertEqual(price, 97.0)


if __name__ == "__main__":
    unittest.main()
