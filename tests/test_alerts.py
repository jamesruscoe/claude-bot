"""FX email alerts — formatting + trigger logic (no SMTP, no network).

Covers the signal-alert formatting, the feed-health streak state machine
(detections-keyed, edge-triggered), and the fail-open end-to-end entry point.
"""
import json
import tempfile
import unittest
from pathlib import Path

from v2 import alerts
from v2 import config as cfg


def _row(symbol="GBPUSD=X", direction="long", score=100, opened=True, candidate=True):
    return {"symbol": symbol, "direction": direction, "score": score, "candidate": candidate,
            "opened": opened, "entry": 1.28400, "stop_loss": 1.27900,
            "tp1": 1.29400, "tp2": 1.29900, "rr": 2.0, "size": "half"}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fxalert_"))
        self._save = (cfg.ALERT_SUBJECT_FILE, cfg.ALERT_BODY_FILE, cfg.ALERT_HEALTH_FILE,
                      cfg.FX_ENABLED, cfg.FX_HEALTH_ZERO_RUNS, cfg.STATE_DIR)
        cfg.ALERT_SUBJECT_FILE = self.tmp / "subj.txt"
        cfg.ALERT_BODY_FILE = self.tmp / "body.txt"
        cfg.ALERT_HEALTH_FILE = self.tmp / "health.json"
        cfg.STATE_DIR = self.tmp
        cfg.FX_ENABLED = True
        cfg.FX_HEALTH_ZERO_RUNS = 3

    def tearDown(self):
        (cfg.ALERT_SUBJECT_FILE, cfg.ALERT_BODY_FILE, cfg.ALERT_HEALTH_FILE,
         cfg.FX_ENABLED, cfg.FX_HEALTH_ZERO_RUNS, cfg.STATE_DIR) = self._save

    def _subject(self):
        return cfg.ALERT_SUBJECT_FILE.read_text(encoding="utf-8").strip()

    def _body(self):
        return cfg.ALERT_BODY_FILE.read_text(encoding="utf-8")

    def _no_alert(self):
        return not cfg.ALERT_BODY_FILE.exists()


class TestSignalFormatting(_Base):
    def test_single_subject_and_fields(self):
        subj, body = alerts.build_signal_alert([_row()])
        self.assertEqual(subj, "FX SIGNAL: GBPUSD LONG @ 1.284")
        for token in ("GBPUSD", "LONG", "score 100", "R:R 2.0", "size half",
                      "1.284", "1.279", "1.294", "1.299"):
            self.assertIn(token, body)
        self.assertIn("Paper trade", body)
        self.assertIn("No live order", body)

    def test_multiple_shows_plus_more(self):
        subj, body = alerts.build_signal_alert([_row(), _row(symbol="EURJPY=X", direction="short")])
        self.assertTrue(subj.startswith("FX SIGNAL: GBPUSD LONG @ 1.284"))
        self.assertIn("(+1 more)", subj)
        self.assertIn("EURJPY  SHORT", body)

    def test_empty_is_none(self):
        self.assertIsNone(alerts.build_signal_alert([]))


class TestSignalEndToEnd(_Base):
    def test_opened_trade_writes_signal_and_clears_streak(self):
        # pre-load a streak so we can prove an open resets it
        cfg.ALERT_HEALTH_FILE.write_text(json.dumps({"consecutive_zero": 2, "alert_active": False}),
                                         encoding="utf-8")
        alerts.evaluate_and_write({"skipped": False, "opened": [{}],
                                   "results": [_row(), _row(symbol="EURUSD=X", opened=False, candidate=True)]})
        self.assertIn("FX SIGNAL: GBPUSD LONG", self._subject())
        self.assertEqual(json.loads(cfg.ALERT_HEALTH_FILE.read_text())["consecutive_zero"], 0)

    def test_not_fx_is_noop(self):
        cfg.FX_ENABLED = False
        alerts.evaluate_and_write({"skipped": False, "opened": [{}], "results": [_row()]})
        self.assertTrue(self._no_alert())


class TestFeedHealth(_Base):
    def test_detected_but_not_taken_is_healthy(self):
        # rows detected (candidate=True) but none opened -> detections>0 -> no alert, streak 0
        alerts.evaluate_and_write({"skipped": False, "opened": [],
                                   "results": [_row(opened=False, candidate=True)]})
        self.assertTrue(self._no_alert())
        self.assertEqual(json.loads(cfg.ALERT_HEALTH_FILE.read_text())["consecutive_zero"], 0)

    def test_all_pairs_stale_alerts_immediately(self):
        # no rows at all (every pair stale-skipped), nothing opened -> feed dead
        alerts.evaluate_and_write({"skipped": False, "opened": [], "results": []})
        self.assertIn("feed looks dead", self._subject())
        self.assertIn("FEED HEALTH", self._body())

    def test_no_data_skip_alerts(self):
        alerts.evaluate_and_write({"skipped": True, "reason": "no data"})
        self.assertIn("feed looks dead", self._subject())

    def test_weekend_skip_is_noop(self):
        alerts.evaluate_and_write({"skipped": True, "reason": "weekend"})
        self.assertTrue(self._no_alert())
        self.assertFalse(cfg.ALERT_HEALTH_FILE.exists())

    def test_consecutive_zero_detection_fires_at_threshold(self):
        # rows present but zero candidates -> quiet, not feed-dead; alert only at N=3
        quiet = {"skipped": False, "opened": [],
                 "results": [{"symbol": "EURUSD=X", "candidate": False, "reject_reason": "no_setup"}]}
        alerts.evaluate_and_write(quiet)
        self.assertTrue(self._no_alert())                    # streak 1
        alerts.evaluate_and_write(quiet)
        self.assertTrue(self._no_alert())                    # streak 2
        alerts.evaluate_and_write(quiet)
        self.assertIn("zero setups detected", self._subject())  # streak 3 == N
        self.assertEqual(json.loads(cfg.ALERT_HEALTH_FILE.read_text())["consecutive_zero"], 3)

    def test_edge_triggered_no_repeat_then_reset(self):
        dead = {"skipped": False, "opened": [], "results": []}
        alerts.evaluate_and_write(dead)                      # fires
        self.assertIn("feed looks dead", self._subject())
        cfg.ALERT_BODY_FILE.unlink()                         # clear to detect a re-fire
        alerts.evaluate_and_write(dead)                      # active -> must NOT re-fire
        self.assertTrue(self._no_alert())
        # a detection clears the active flag and streak
        alerts.evaluate_and_write({"skipped": False, "opened": [], "results": [_row(opened=False)]})
        st = json.loads(cfg.ALERT_HEALTH_FILE.read_text())
        self.assertEqual(st["consecutive_zero"], 0)
        self.assertFalse(st["alert_active"])


class TestMisc(_Base):
    def test_test_alert_writes_files(self):
        alerts.write_test_alert()
        self.assertIn("TEST", self._subject())
        self.assertIn("delivery works", self._body())

    def test_fail_open_on_malformed_payload(self):
        # results is not a list of dicts -> must not raise
        alerts.evaluate_and_write({"skipped": False, "opened": [], "results": [None, 5]})
        # no crash is the assertion; may or may not write a file


if __name__ == "__main__":
    unittest.main()
