"""OANDA v20 practice adapter + Phase A split/baseline plumbing — all against
MOCKED responses (no network). Covers: candle parsing, mid-bar + measured-spread
extraction, the fallback-vs-measured spread, pagination, the locked train/holdout
split, and the deterministic bootstrap CI. Also a static scope guard that the
adapter never references an orders/positions endpoint."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_data import Bar
from v2 import config as cfg
from v2 import oanda_baseline as ob
from v2.oanda_source import OANDASource, _parse_oanda_time


def _candle(dt: datetime, mid_c: float, spread_pips: float, pip: float) -> dict:
    half = spread_pips * pip / 2
    o, h, l, c = mid_c - 0.001, mid_c + 0.002, mid_c - 0.002, mid_c
    def px(x):  # bid = mid - half, ask = mid + half
        return {"o": f"{x[0]:.5f}", "h": f"{x[1]:.5f}", "l": f"{x[2]:.5f}", "c": f"{x[3]:.5f}"}
    quad = (o, h, l, c)
    return {
        "complete": True,
        "time": dt.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
        "volume": 1000,
        "mid": px(quad),
        "bid": px(tuple(v - half for v in quad)),
        "ask": px(tuple(v + half for v in quad)),
    }


class TestParsing(unittest.TestCase):
    def test_parse_nanosecond_time(self):
        ms = _parse_oanda_time("2020-01-01T22:00:00.000000000Z")
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        self.assertEqual((dt.year, dt.month, dt.day, dt.hour), (2020, 1, 1, 22))

    def test_parse_no_fraction(self):
        ms = _parse_oanda_time("2019-06-15T13:30:00Z")
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        self.assertEqual((dt.year, dt.month, dt.day, dt.minute), (2019, 6, 15, 30))


class TestCandleExtraction(unittest.TestCase):
    def setUp(self):
        self.src = OANDASource(token="dummy")

    def test_mid_bars_and_measured_spread_nonjpy(self):
        pip = cfg.fx_pip_size("EURUSD=X")  # 0.0001
        base = datetime(2020, 1, 1, tzinfo=timezone.utc)
        candles = [_candle(base + timedelta(days=i), 1.10 + i * 0.001, 1.2, pip)
                   for i in range(5)]
        rows = self.src._candles_to_rows("EURUSD=X", candles)
        bars = self.src._rows_to_bars("EURUSD=X", rows)
        self.assertEqual(len(bars), 5)
        # mid close preserved
        self.assertAlmostEqual(bars[0].c, 1.10, places=5)
        # spread ask.c - bid.c == 1.2 pips (within rounding)
        stats = self.src.measured_spread_stats("EURUSD=X")
        self.assertAlmostEqual(stats["median"], 1.2, places=1)
        self.assertEqual(stats["n"], 5)

    def test_measured_spread_jpy_pip_scale(self):
        pip = cfg.fx_pip_size("USDJPY=X")  # 0.01
        base = datetime(2020, 1, 1, tzinfo=timezone.utc)
        candles = [_candle(base + timedelta(days=i), 110.0 + i, 1.5, pip) for i in range(4)]
        rows = self.src._candles_to_rows("USDJPY=X", candles)
        self.src._rows_to_bars("USDJPY=X", rows)
        stats = self.src.measured_spread_stats("USDJPY=X")
        self.assertAlmostEqual(stats["median"], 1.5, places=1)

    def test_incomplete_candles_dropped_in_pagination(self):
        # _candles_to_rows keeps all; the pagination layer filters `complete`.
        pip = cfg.fx_pip_size("EURUSD=X")
        c = _candle(datetime(2020, 1, 1, tzinfo=timezone.utc), 1.10, 1.0, pip)
        c["complete"] = False
        rows = self.src._candles_to_rows("EURUSD=X", [c])
        self.assertEqual(len(rows), 1)  # extraction is complete-agnostic by design

    def test_spread_pips_fallback_before_fetch(self):
        # No fetch yet -> falls back to the assumed conservative constant.
        self.assertEqual(self.src.spread_pips("EURGBP=X"), cfg.fx_spread_pips("EURGBP=X"))


class TestPagination(unittest.TestCase):
    def test_fetch_daily_paginates_and_dedups(self):
        src = OANDASource(token="dummy")
        pip = cfg.fx_pip_size("EURUSD=X")
        base = datetime(2018, 1, 1, tzinfo=timezone.utc)
        all_candles = [_candle(base + timedelta(days=i), 1.10 + i * 0.0005, 1.0, pip)
                       for i in range(30)]
        # A full page (== cap) forces a second request; the overlap-by-1 exercises
        # de-dup. Shrink the cap so 20 candles is a "full" page.
        pages = [all_candles[:20], all_candles[19:]]
        calls = {"n": 0}

        async def fake_page(instrument, *, granularity, start, count):
            i = calls["n"]
            calls["n"] += 1
            return pages[i] if i < len(pages) else []

        src._get_candles_page = fake_page          # type: ignore[assignment]
        src._read_cache = lambda *a, **k: None      # type: ignore[assignment]
        src._write_cache = lambda *a, **k: None     # type: ignore[assignment]
        orig_cap = cfg.OANDA_MAX_CANDLES
        cfg.OANDA_MAX_CANDLES = 20
        try:
            bars = asyncio.run(src.fetch_daily("EURUSD=X"))
        finally:
            cfg.OANDA_MAX_CANDLES = orig_cap
        self.assertEqual(len(bars), 30)             # 20 + 11 - 1 overlap deduped
        self.assertEqual(calls["n"], 2)             # exactly two pages requested
        self.assertTrue(all(bars[i].t < bars[i + 1].t for i in range(len(bars) - 1)))

    def test_fetch_daily_unmapped_symbol(self):
        src = OANDASource(token="dummy")
        self.assertEqual(asyncio.run(src.fetch_daily("XAUUSD=X")), [])


class TestSplit(unittest.TestCase):
    def _bars(self, start: datetime, n: int) -> list[Bar]:
        return [Bar(t=int((start + timedelta(days=i)).timestamp() * 1000),
                    o=1.1, h=1.11, l=1.09, c=1.10, v=0) for i in range(n)]

    def test_boundary_splits_before_and_after(self):
        bars = self._bars(datetime(2020, 12, 20, tzinfo=timezone.utc), 30)
        train, holdout = ob.split_train_holdout(bars)
        self.assertTrue(all(b.dt < cfg.train_holdout_boundary() for b in train))
        self.assertTrue(all(b.dt >= cfg.train_holdout_boundary() for b in holdout))
        self.assertEqual(len(train) + len(holdout), 30)
        # 2020-12-20..2020-12-31 = 12 train, 2021-01-01.. = 18 holdout
        self.assertEqual(len(train), 12)

    def test_boundary_is_the_locked_date(self):
        self.assertEqual(cfg.TRAIN_HOLDOUT_BOUNDARY, "2021-01-01")


class TestBootstrap(unittest.TestCase):
    def test_deterministic(self):
        vals = [3.0, -1.0, -1.0, 3.0, -1.0, -1.0, 3.0, -1.0]
        a = ob.bootstrap_mean_ci(vals, iters=2000)
        b = ob.bootstrap_mean_ci(vals, iters=2000)
        self.assertEqual(a, b)

    def test_lower_bound_below_mean(self):
        vals = [3.0, -1.0, -1.0, -1.0, 3.0, -1.0, -1.0, -1.0, 3.0, -1.0]
        ci = ob.bootstrap_mean_ci(vals, iters=3000)
        self.assertLess(ci["one_sided_95_lower"], ci["mean"])
        self.assertLessEqual(ci["two_sided_95_lower"], ci["one_sided_95_lower"] + 1e-6)

    def test_none_for_thin_sample(self):
        self.assertIsNone(ob.bootstrap_mean_ci([1.0]))


class TestScopeGuard(unittest.TestCase):
    def test_adapter_never_references_order_endpoints(self):
        src = Path(__file__).resolve().parent.parent / "v2" / "oanda_source.py"
        text = src.read_text(encoding="utf-8").lower()
        for forbidden in ("/orders", "/trades", "/positions", "order placement"):
            self.assertNotIn(forbidden, text,
                             f"adapter must not reference {forbidden!r}")
        # host is the practice endpoint only
        self.assertIn("api-fxpractice", cfg.OANDA_HOST)


if __name__ == "__main__":
    unittest.main()
