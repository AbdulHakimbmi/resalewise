"""Offline tests for the pure parts of the benchmark scripts (no database needed)."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bench"))

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/x")
os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:1")
os.environ.setdefault("TOKEN_SECRET", "unit-test-secret-0123456789abcdef")

import run_bench  # noqa: E402


class IndexEffectTests(unittest.TestCase):
    def write(self, d, workloads):
        path = os.path.join(d, "benchmark.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"generated_at": "2026-10-26T00:00:00+00:00", "workloads": workloads}, fh)
        return path

    def test_no_baseline_file_means_no_slowdown_figures(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(run_bench, "OUT", os.path.join(d, "missing.json")):
            self.assertIsNone(run_bench.index_effect({"W1": {"mongodb": {"p50_ms": 5.0}}}))

    def test_slowdown_is_median_without_indexes_over_median_with_indexes(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d, [{"id": "W1", "latency": {"mongodb": {"p50_ms": 2.0},
                                                           "postgres_flat": {"p50_ms": 0.5}}}])
            with mock.patch.object(run_bench, "OUT", path):
                got = run_bench.index_effect({"W1": {"mongodb": {"p50_ms": 10.0},
                                                     "postgres_flat": {"p50_ms": 5.0}}})
        self.assertEqual(got["slowdown_x"], {"W1": {"mongodb": 5.0, "postgres_flat": 10.0}})
        self.assertEqual(got["baseline_generated_at"], "2026-10-26T00:00:00+00:00")

    def test_workloads_missing_from_the_baseline_are_skipped_not_guessed(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d, [{"id": "W1", "latency": {"mongodb": {"p50_ms": 2.0}}}])
            with mock.patch.object(run_bench, "OUT", path):
                got = run_bench.index_effect({"W9": {"mongodb": {"p50_ms": 1.0}}, "W1": {"postgres_flat": {"p50_ms": 1.0}}})
        self.assertEqual(got["slowdown_x"], {})

    def test_workloads_read_their_table_and_collection_from_module_settings(self):
        """--no-index swaps these two names; the SQL and the MongoDB calls must follow them."""
        queries = []

        class Cur:
            def execute(self, sql, params=None):
                queries.append(sql)

            def fetchone(self):
                return None

        class Mongo:
            def __getitem__(self, name):
                queries.append("mongo:" + name)
                return mock.MagicMock()

        ctx = {"max_txn_id": 10, "combos": [], "cutoff_24m": None}
        with mock.patch.object(run_bench, "pg", return_value=Cur()), \
                mock.patch.object(run_bench, "mongo", return_value=Mongo()), \
                mock.patch.object(run_bench, "FLAT", "bench_txn_noidx"), \
                mock.patch.object(run_bench, "TXN", "transactions_noidx"):
            run_bench.W0(ctx).postgres_flat({"id": 1})
            run_bench.W0(ctx).mongodb({"id": 1})
        self.assertIn("bench_txn_noidx", queries[0])
        self.assertEqual(queries[1], "mongo:transactions_noidx")


if __name__ == "__main__":
    unittest.main()
