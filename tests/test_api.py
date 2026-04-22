import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import api  # noqa: E402


class FakeSnapshotsQuery:
    def __init__(self, rows):
        self.rows = rows
        self.ranges = []
        self.pipeline_tag = None

    def select(self, *_args, **_kwargs):
        return self

    def gte(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def eq(self, _column, value):
        self.pipeline_tag = value
        return self

    def range(self, start, end):
        self.ranges.append((start, end))
        self._start = start
        self._end = end
        return self

    def execute(self):
        rows = self.rows
        if self.pipeline_tag:
            rows = [row for row in rows if row["pipeline_tag"] == self.pipeline_tag]
        return SimpleNamespace(data=rows[self._start : self._end + 1])


class FakeSupabase:
    def __init__(self, rows):
        self.query = FakeSnapshotsQuery(rows)

    def table(self, name):
        assert name == "model_snapshots"
        return self.query


class TrendingTests(unittest.TestCase):
    def test_trending_fetches_all_pages_before_ranking(self):
        rows = []
        for i in range(api._PAGE_SIZE + 2):
            model_id = f"org/model-{i}"
            rows.append(
                {
                    "model_id": model_id,
                    "snapshot_date": "2026-04-01",
                    "likes": 1,
                    "pipeline_tag": "text-generation",
                }
            )
            rows.append(
                {
                    "model_id": model_id,
                    "snapshot_date": "2026-04-22",
                    "likes": i,
                    "pipeline_tag": "text-generation",
                }
            )
        # The best model is beyond the first PostgREST-sized page; without
        # explicit pagination this would be invisible to the endpoint.
        fake_sb = FakeSupabase(rows)

        with patch.object(api, "get_supabase", return_value=fake_sb):
            result = api.get_trending(
                pipeline_tag="text-generation",
                days=30,
                limit=1,
            )

        self.assertEqual(result[0]["model_id"], f"org/model-{api._PAGE_SIZE + 1}")
        self.assertGreater(len(fake_sb.query.ranges), 1)

    def test_fetch_range_pages_allows_exact_cap_without_false_overflow(self):
        rows = [{"model_id": "m", "pipeline_tag": "text-generation"}] * api._PAGE_SIZE
        query = FakeSnapshotsQuery(rows)

        fetched = api._fetch_range_pages(query, max_rows=api._PAGE_SIZE + 1)

        self.assertEqual(len(fetched), api._PAGE_SIZE)
        self.assertEqual(query.ranges[-1], (api._PAGE_SIZE, api._PAGE_SIZE))

    def test_trending_rejects_overflow_instead_of_returning_partial_ranking(self):
        rows = []
        for i in range(api._PAGE_SIZE + 1):
            rows.append(
                {
                    "model_id": f"org/model-{i}",
                    "snapshot_date": "2026-04-01",
                    "likes": 1,
                    "pipeline_tag": "text-generation",
                }
            )
        fake_sb = FakeSupabase(rows)

        with patch.object(api, "_TRENDING_HARD_ROW_CAP", api._PAGE_SIZE), patch.object(
            api, "get_supabase", return_value=fake_sb
        ):
            with self.assertRaises(HTTPException) as ctx:
                api.get_trending(pipeline_tag="text-generation", days=30, limit=1)

        self.assertEqual(ctx.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
