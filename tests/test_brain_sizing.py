"""Phase 3 graduated probationary sizing (fixes the audit cold-start deadlock)."""
import unittest

from v2 import brain


def _retrieval(*, n_closed=0, wins=0, losses=0, wr=None, avg_r=None, meaningful=False):
    return {
        "symbol_stats": {"symbol": "EURUSD=X", "n_closed": n_closed, "wins": wins,
                         "losses": losses, "win_rate": wr, "avg_r": avg_r,
                         "meaningful": meaningful},
        "memories": [], "lessons": [],
    }


def _cand(score):
    return {"symbol": "EURUSD=X", "score": score, "direction": "long",
            "setups": ["ob_retest"] if score < 100 else ["ob_retest", "bos_retest"],
            "rr": 2.0}


class TestGraduatedSizing(unittest.TestCase):
    def test_single_setup_cold_start_is_taken_not_skipped(self):
        # The audit deadlock: this used to be a hard skip. Now it's taken tiny.
        d = brain._judge_deterministic(_cand(50), _retrieval())
        self.assertTrue(d["take"])
        self.assertEqual(d["size"], "quarter")

    def test_dual_confluence_thin_sample_half(self):
        d = brain._judge_deterministic(_cand(100), _retrieval(n_closed=3, wins=2, losses=1))
        self.assertTrue(d["take"])
        self.assertEqual(d["size"], "half")

    def test_strong_meaningful_record_full(self):
        d = brain._judge_deterministic(
            _cand(50), _retrieval(n_closed=10, wins=7, losses=3, wr=0.7, avg_r=0.8, meaningful=True))
        self.assertTrue(d["take"])
        self.assertEqual(d["size"], "full")

    def test_proven_bad_is_hard_skipped(self):
        d = brain._judge_deterministic(
            _cand(100), _retrieval(n_closed=10, wins=2, losses=8, wr=0.2, avg_r=-0.5, meaningful=True))
        self.assertFalse(d["take"])
        self.assertEqual(d["size"], "none")

    def test_ramps_with_decided_count(self):
        cold = brain._judge_deterministic(_cand(100), _retrieval())
        thin = brain._judge_deterministic(_cand(100), _retrieval(n_closed=3, wins=2, losses=1))
        self.assertEqual(cold["size"], "quarter")
        self.assertEqual(thin["size"], "half")


if __name__ == "__main__":
    unittest.main()
