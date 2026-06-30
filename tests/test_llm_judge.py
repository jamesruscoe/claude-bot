"""Phase 4 Claude judge: strict-JSON parsing, size mapping, cost (offline)."""
import unittest

from v2 import llm


class TestVerdictParsing(unittest.TestCase):
    def test_take_with_multiplier_maps_to_bucket(self):
        d = llm.parse_verdict({"verdict": "take", "confidence": "high",
                               "size_multiplier": 1.0, "reason": "strong"})
        self.assertTrue(d["take"])
        self.assertEqual(d["size"], "full")
        self.assertEqual(d["confidence"], "high")

    def test_skip_forces_size_none(self):
        d = llm.parse_verdict({"verdict": "skip", "confidence": "low",
                               "size_multiplier": 0.9, "reason": "no edge"})
        self.assertFalse(d["take"])
        self.assertEqual(d["size"], "none")

    def test_malformed_returns_none(self):
        self.assertIsNone(llm.parse_verdict({"confidence": "high"}))  # no verdict
        self.assertIsNone(llm.parse_verdict(None))

    def test_size_multiplier_buckets(self):
        self.assertEqual(llm._size_from_multiplier(0.0), "none")
        self.assertEqual(llm._size_from_multiplier(0.2), "quarter")
        self.assertEqual(llm._size_from_multiplier(0.5), "half")
        self.assertEqual(llm._size_from_multiplier(0.8), "full")
        self.assertEqual(llm._size_from_multiplier("garbage"), "half")  # safe default


class TestParseJson(unittest.TestCase):
    def test_strips_code_fences(self):
        out = llm._parse_json('```json\n{"verdict": "take"}\n```')
        self.assertEqual(out, {"verdict": "take"})

    def test_bad_json_returns_none(self):
        self.assertIsNone(llm._parse_json("not json at all"))


class TestCost(unittest.TestCase):
    def test_haiku_pricing(self):
        # 1M in @ $1 + 1M out @ $5 = $6.00
        self.assertAlmostEqual(llm.estimate_cost("claude-haiku-4-5", 1_000_000, 1_000_000), 6.0)

    def test_batch_is_half_price(self):
        full = llm.estimate_cost("claude-haiku-4-5", 1_000_000, 0)
        batch = llm.estimate_cost("claude-haiku-4-5", 1_000_000, 0, batch=True)
        self.assertAlmostEqual(batch, full * 0.5)

    def test_unknown_model_is_zero(self):
        self.assertEqual(llm.estimate_cost("mystery", 1000, 1000), 0.0)


if __name__ == "__main__":
    unittest.main()
