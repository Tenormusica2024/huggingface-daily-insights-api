import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import crawl_arena  # noqa: E402


class FakeTable:
    def __init__(self):
        self.rows = []

    def upsert(self, row, on_conflict):
        assert on_conflict == "snapshot_date,model_name"
        self.rows.append(row)
        return self

    def execute(self):
        return None


class FakeSupabase:
    def __init__(self):
        self.rankings = FakeTable()

    def table(self, name):
        assert name == "arena_rankings"
        return self.rankings


class ArenaJsonModeTests(unittest.TestCase):
    def test_export_json_does_not_touch_supabase(self):
        rows = [
            {
                "snapshot_date": "2026-04-20",
                "model_name": "model-a",
                "rank": 1,
                "elo_score": 1200,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "arena.json"
            with patch.object(
                crawl_arena,
                "latest_pkl_files",
                return_value=[("elo_results_20260420.pkl", date(2026, 4, 20))],
            ), patch.object(
                crawl_arena, "download_and_parse_pkl", return_value=rows
            ), patch.object(crawl_arena, "get_supabase") as get_supabase:
                count = crawl_arena.export_rankings_json(out_path)

            self.assertEqual(count, 1)
            self.assertEqual(json.loads(out_path.read_text(encoding="utf-8")), rows)
            get_supabase.assert_not_called()

    def test_import_json_upserts_without_loading_pickle(self):
        rows = [
            {
                "snapshot_date": "2026-04-20",
                "model_name": "model-a",
                "rank": 1,
                "elo_score": 1200,
            }
        ]
        fake_sb = FakeSupabase()
        with tempfile.TemporaryDirectory() as tmp:
            in_path = Path(tmp) / "arena.json"
            in_path.write_text(json.dumps(rows), encoding="utf-8")
            with patch.object(crawl_arena, "get_supabase", return_value=fake_sb), patch.object(
                crawl_arena, "download_and_parse_pkl"
            ) as parse_pkl:
                ok, err = crawl_arena.import_rankings_json(in_path)

        self.assertEqual((ok, err), (1, 0))
        self.assertEqual(fake_sb.rankings.rows, rows)
        parse_pkl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
