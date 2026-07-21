"""P0 accounting plumbing for the multi-pattern detector (PATTERN_SCOPE.md):
per-pattern ledger tag, forward-only confidence tiers, pattern attribution, and
the email annotation. No new pattern is enabled here — behaviour is unchanged.
"""
import tempfile
import unittest
from pathlib import Path

from v2 import alerts, confidence
from v2 import config as cfg
from v2 import store
from v2.pipeline import _primary_pattern


def _cand(symbol="EURUSD=X", direction="long", setups=("ob_retest",)):
    return {"symbol": symbol, "direction": direction, "stop_loss": 1.0,
            "tp1": 1.1, "tp2": 1.2, "setups": list(setups)}


class _DB(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="patp0_"))
        self._save = (cfg.STATE_DIR, cfg.DB_PATH, cfg.JOURNAL_DIR, cfg.LESSONS_DIR,
                      cfg.CACHE_DIR, cfg.FX_CONF_MIN_N, cfg.FX_CONF_PROVEN_N)
        cfg.STATE_DIR = self.tmp
        cfg.DB_PATH = self.tmp / "ledger.db"
        cfg.JOURNAL_DIR = self.tmp / "journal"
        cfg.LESSONS_DIR = self.tmp / "lessons"
        cfg.CACHE_DIR = self.tmp / "cache"
        cfg.FX_CONF_MIN_N = 3       # shrink thresholds so tiers are cheap to reach
        cfg.FX_CONF_PROVEN_N = 10
        store.init_db()

    def tearDown(self):
        (cfg.STATE_DIR, cfg.DB_PATH, cfg.JOURNAL_DIR, cfg.LESSONS_DIR,
         cfg.CACHE_DIR, cfg.FX_CONF_MIN_N, cfg.FX_CONF_PROVEN_N) = self._save

    def _seed(self, pattern, r_values, *, source="forward"):
        for i, r in enumerate(r_values):
            t = store.open_trade(None, _cand(), fill=1.05,
                                 opened_at=f"2026-01-{(i % 27) + 1:02d}T00:00:00+00:00",
                                 source=source, pattern=pattern)
            outcome = "WIN_TP2" if r > 0 else "LOSS"
            store.update_trade_close(t["id"], outcome, 1.2,
                                     "2026-02-01T00:00:00+00:00", pnl_r=r, raw_r=r)


class TestLedgerTag(_DB):
    def test_pattern_column_persists_and_queries(self):
        store.open_trade(None, _cand(), 1.05, source="forward", pattern="range_breakout")
        store.open_trade(None, _cand(), 1.05, source="forward", pattern="ob_retest")
        self.assertEqual(len(store.trades_by_pattern("range_breakout")), 1)
        self.assertIn("range_breakout", store.distinct_patterns())
        self.assertIn("ob_retest", store.distinct_patterns())

    def test_default_tag_is_ob_retest(self):
        store.open_trade(None, _cand(), 1.05)  # no pattern kwarg
        self.assertEqual(store.trades_by_pattern("ob_retest")[0]["pattern"], "ob_retest")


class TestPrimaryPattern(unittest.TestCase):
    def test_priority_and_fallback(self):
        self.assertEqual(_primary_pattern(["ob_retest", "bos_retest"]), "ob_retest")
        self.assertEqual(_primary_pattern(["bos_retest"]), "bos_retest")
        self.assertEqual(_primary_pattern([]), "ob_retest")
        self.assertEqual(_primary_pattern(None), "ob_retest")


class TestConfidenceTiers(_DB):
    def test_unproven_below_min_n(self):
        self._seed("double_bottom", [0.4, -1.0])          # n=2 < 3
        c = confidence.confidence_for("double_bottom")
        self.assertEqual(c["tier"], "unproven")
        self.assertIn("unproven", confidence.label("double_bottom"))

    def test_provisional_between_thresholds(self):
        self._seed("double_bottom", [0.4] * 5)            # 3 <= 5 < 10
        self.assertEqual(confidence.confidence_for("double_bottom")["tier"], "provisional")

    def test_proven_when_lower_bound_positive(self):
        self._seed("range_breakout", [0.4] * 12)          # n>=10, LB=0.4>0
        c = confidence.confidence_for("range_breakout")
        self.assertEqual(c["tier"], "proven")
        self.assertGreater(c["lower_bound"], 0)

    def test_not_positive_when_lower_bound_nonpositive(self):
        self._seed("range_breakout", [-0.1] * 12)         # n>=10, LB<=0
        self.assertEqual(confidence.confidence_for("range_breakout")["tier"], "not_positive")

    def test_confidence_is_forward_only(self):
        # backfill (in-sample) trades must NOT count toward confidence
        self._seed("hns", [0.4] * 20, source="backfill")
        c = confidence.confidence_for("hns")
        self.assertEqual(c["n"], 0)
        self.assertEqual(c["tier"], "unproven")

    def test_report_text_runs(self):
        self._seed("double_bottom", [0.4] * 5)
        txt = confidence.pattern_report_text()
        self.assertIn("double_bottom", txt)
        self.assertIn("Per-pattern expectancy", txt)


class TestEmailAnnotation(unittest.TestCase):
    def _row(self, **extra):
        r = {"symbol": "GBPUSD=X", "direction": "long", "score": 100, "rr": 2.0,
             "size": "half", "entry": 1.284, "stop_loss": 1.279, "tp1": 1.294,
             "tp2": 1.299}
        r.update(extra)
        return r

    def test_subject_and_body_include_pattern_and_confidence(self):
        row = self._row(pattern="double_bottom", pattern_confidence="unproven (n=7 fwd)")
        subj, body = alerts.build_signal_alert([row])
        self.assertIn("[double_bottom · unproven]", subj)
        self.assertIn("pattern: double_bottom", body)
        self.assertIn("confidence: unproven (n=7 fwd)", body)

    def test_backward_compatible_without_pattern(self):
        # a row with no pattern (equities / old path) keeps the plain subject
        subj, _ = alerts.build_signal_alert([self._row()])
        self.assertEqual(subj, "FX SIGNAL: GBPUSD LONG @ 1.284")


if __name__ == "__main__":
    unittest.main()
