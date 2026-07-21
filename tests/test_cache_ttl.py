"""Cache TTL is keyed on the fetch time stored inside the file, not mtime — the
bug that froze the FX bot's bars when state was restored from the branch (git
checkout resets mtime, so an mtime-based TTL looked permanently fresh)."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from market_data import Bar
from v2 import config as cfg
from v2 import datasource


class TestCacheTTL(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="botv2_cache_"))
        cfg.CACHE_DIR = self.tmp
        self.src = datasource.FXSource()
        self.bars = [Bar(t=1_700_000_000_000, o=1.1, h=1.2, l=1.0, c=1.15, v=0)]

    def test_fresh_cache_is_returned(self):
        self.src._write_cache("EURUSD=X", "1d", self.bars)
        got = self.src._read_cache("EURUSD=X", "1d", ttl=900)
        self.assertIsNotNone(got)
        self.assertEqual(len(got), 1)

    def test_expired_by_fetched_at_even_when_mtime_is_now(self):
        # Simulate a git-restored cache: old fetch time inside, but mtime = now.
        self.src._write_cache("EURUSD=X", "1d", self.bars)
        p = self.src._cache_path("EURUSD=X", "1d")
        data = json.loads(p.read_text(encoding="utf-8"))
        data["fetched_at"] = time.time() - 10_000            # fetched long ago
        p.write_text(json.dumps(data), encoding="utf-8")
        os.utime(p, None)                                    # mtime -> now (git checkout)
        self.assertIsNone(self.src._read_cache("EURUSD=X", "1d", ttl=900))

    def test_legacy_bare_list_is_invalidated(self):
        p = self.src._cache_path("EURUSD=X", "1d")
        p.write_text(json.dumps([{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}]),
                     encoding="utf-8")
        self.assertIsNone(self.src._read_cache("EURUSD=X", "1d", ttl=900))


if __name__ == "__main__":
    unittest.main()
