"""
LMArena ELO ランキング日次クロールスクリプト
毎日 UTC 00:00 に GitHub Actions から実行される

データソース: lmarena-ai/lmarena-leaderboard HF Space
              elo_results_YYYYMMDD.pkl → ELO ランキング
保存先: Supabase (arena_rankings テーブル)

注意: HF Space のデータ更新頻度はまちまちで、数週間単位での更新になる場合がある。
     既存 snapshot_date はスキップするため、重複インポートは発生しない。

運用上の安全策:
     GitHub Actions では pickle の解析を Supabase secrets なしの step で行い、
     生成済み JSON を次 step で import する。pickle が信頼境界をまたぐため、
     secrets を持つプロセスでは pickle.load しない。
"""

import argparse
import json
import logging
import pickle
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

from huggingface_hub import hf_hub_download, list_repo_files
from supabase import Client

from config import ERROR_RATE_THRESHOLD
from db import get_supabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# lmarena-ai/lmarena-leaderboard Space の repo_id
_HF_SPACE_ID = "lmarena-ai/lmarena-leaderboard"
# ELO pkl ファイル名のパターン (例: elo_results_20250829.pkl)
_PKL_PATTERN = re.compile(r"elo_results_(\d{8})\.pkl")
# text-full カテゴリのみ取得（テキスト会話の総合 ELO）
_CATEGORY_PATH = ["text", "full", "leaderboard_table_df"]
_DEFAULT_EXPORT_PATH = Path("arena_rankings_import.json")
_ARENA_ROW_KEYS = {"snapshot_date", "model_name", "rank", "elo_score"}
_MAX_IMPORT_ROWS = 10_000
_MAX_MODEL_NAME_LEN = 300


def list_elo_pkl_files() -> list[tuple[str, date]]:
    """
    HF Space 内の elo_results_*.pkl を列挙し、日付でソートして返す。
    戻り値: [(filename, snapshot_date), ...] の昇順リスト
    """
    files = list_repo_files(_HF_SPACE_ID, repo_type="space")
    results = []
    for fname in files:
        # list_repo_files はサブディレクトリ配下のパスも返しうるため basename で判定する
        basename = fname.rsplit("/", 1)[-1]
        m = _PKL_PATTERN.match(basename)
        if m:
            d = date(int(m.group(1)[:4]), int(m.group(1)[4:6]), int(m.group(1)[6:8]))
            results.append((fname, d))
    results.sort(key=lambda x: x[1])
    return results


def get_imported_dates(sb: Client) -> set[str]:
    """Supabase に既にインポート済みの snapshot_date セットを返す。"""
    resp = (
        sb.table("arena_rankings")
        .select("snapshot_date")
        .order("snapshot_date", desc=True)
        .limit(500)
        .execute()
    )
    return {row["snapshot_date"] for row in resp.data}


def latest_pkl_files(limit: int = 3) -> list[tuple[str, date]]:
    """
    最新の ELO pkl ファイルを新しい順に最大 limit 件返す。

    secretless export mode では DB に接続せず、重複は import 時の upsert に任せる。
    """
    pkl_files = list_elo_pkl_files()
    if not pkl_files:
        return []
    return list(reversed(pkl_files))[:limit]


def download_and_parse_pkl(filename: str, snapshot_date: date) -> list[dict]:
    """
    HF Space から pkl をダウンロードし、ELO ランキングを抽出する。
    戻り値: [{"snapshot_date", "model_name", "rank", "elo_score"}, ...]
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = hf_hub_download(
            repo_id=_HF_SPACE_ID,
            filename=filename,
            repo_type="space",
            local_dir=tmpdir,
        )
        with open(local_path, "rb") as f:
            # SECURITY: pickle.load は任意コード実行の典型経路。
            # 信頼の前提は「lmarena-ai/lmarena-leaderboard HF Space 運営」のみ。
            # 上流が侵害された場合は本プロセスで RCE に至るため、GitHub Actions では
            # この関数を Supabase secrets なしの step だけで呼び、DB 書き込みとは分離する。
            data = pickle.load(f)  # noqa: S301

    # text/full/leaderboard_table_df を取得
    # 構造: data[category][subcategory]["leaderboard_table_df"] = DataFrame
    # 列: rating, variance, rating_upper, rating_lower, num_battles, final_ranking
    try:
        obj = data
        for key in _CATEGORY_PATH:
            obj = obj[key]
        df = obj
    except (KeyError, TypeError) as e:
        logger.error(f"Unexpected pkl structure in {filename}: {e}")
        return []

    rows = []
    for model_name, row in df.iterrows():
        rows.append({
            "snapshot_date": snapshot_date.isoformat(),
            "model_name": str(model_name),
            "rank": int(row["final_ranking"]),
            "elo_score": int(row["rating"]),
        })
    return rows


def export_rankings_json(out_path: Path, max_files: int = 3) -> int:
    """
    最新 pkl を secretless に解析し、DB import 用 JSON に書き出す。

    戻り値は抽出できたランキング行数。重複 snapshot_date は import 時の upsert で吸収する。
    """
    logger.info("Listing latest elo_results_*.pkl files from HF Space...")
    files = latest_pkl_files(limit=max_files)
    if not files:
        logger.warning("No elo_results_*.pkl files found in HF Space.")
        out_path.write_text("[]", encoding="utf-8")
        return 0

    logger.info(f"Exporting rankings from {len(files)} pkl file(s); latest={files[0][0]}")
    all_rows: list[dict] = []
    for filename, snapshot_date in files:
        logger.info(f"Parsing {filename} (snapshot_date={snapshot_date}) ...")
        rows = download_and_parse_pkl(filename, snapshot_date)
        if not rows:
            logger.warning(f"  No data extracted from {filename}, skipping.")
            continue
        logger.info(f"  Extracted {len(rows)} model rankings")
        all_rows.extend(rows)

    normalized_rows = validate_rankings_rows(all_rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(normalized_rows, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Wrote {len(all_rows)} ranking rows to {out_path}")
    return len(normalized_rows)


def validate_rankings_rows(rows: object) -> list[dict]:
    """
    secretless parse job から渡される JSON artifact の schema / size を検証する。

    pickle 側が侵害されても、secrets を持つ import step ではこの正規化済み
    フィールドだけを DB に upsert する。
    """
    if not isinstance(rows, list):
        raise ValueError("Arena rankings JSON must be a list")
    if len(rows) > _MAX_IMPORT_ROWS:
        raise ValueError(f"Arena rankings JSON has too many rows: {len(rows)}")

    normalized: list[dict] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"Row {idx} must be an object")
        if set(row.keys()) != _ARENA_ROW_KEYS:
            raise ValueError(f"Row {idx} has unexpected keys: {sorted(row.keys())}")

        snapshot_date = row["snapshot_date"]
        model_name = row["model_name"]
        rank = row["rank"]
        elo_score = row["elo_score"]

        if not isinstance(snapshot_date, str):
            raise ValueError(f"Row {idx} snapshot_date must be a string")
        # Validate strict ISO date; store as original YYYY-MM-DD string.
        parsed_date = date.fromisoformat(snapshot_date)
        if snapshot_date != parsed_date.isoformat():
            raise ValueError(f"Row {idx} snapshot_date must be YYYY-MM-DD")

        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError(f"Row {idx} model_name must be a non-empty string")
        if len(model_name) > _MAX_MODEL_NAME_LEN:
            raise ValueError(f"Row {idx} model_name is too long")

        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
            raise ValueError(f"Row {idx} rank must be a positive integer")
        if not isinstance(elo_score, int) or isinstance(elo_score, bool) or not (0 <= elo_score <= 10_000):
            raise ValueError(f"Row {idx} elo_score must be an integer in range 0..10000")

        normalized.append(
            {
                "snapshot_date": snapshot_date,
                "model_name": model_name.strip(),
                "rank": rank,
                "elo_score": elo_score,
            }
        )
    return normalized


def upsert_rankings(sb: Client, rows: list[dict]) -> tuple[int, int]:
    """
    arena_rankings テーブルに upsert する。
    戻り値: (成功件数, エラー件数)
    """
    ok = err = 0
    for row in rows:
        try:
            sb.table("arena_rankings").upsert(
                row, on_conflict="snapshot_date,model_name"
            ).execute()
            ok += 1
        except Exception as e:
            logger.warning(f"  Failed to upsert {row['model_name']}: {e}")
            err += 1
    return ok, err


def import_rankings_json(in_path: Path) -> tuple[int, int]:
    """export_rankings_json が生成した JSON を Supabase に upsert する。"""
    rows = json.loads(in_path.read_text(encoding="utf-8"))
    rows = validate_rankings_rows(rows)

    sb = get_supabase()
    ok, err = upsert_rankings(sb, rows)
    logger.info(f"Imported arena rankings: ok={ok}, err={err}")

    if ok + err > 0:
        error_rate = err / (ok + err)
        if error_rate > ERROR_RATE_THRESHOLD:
            logger.error(
                f"Error rate {error_rate:.1%} exceeded {ERROR_RATE_THRESHOLD:.0%} — exiting with code 1"
            )
            sys.exit(1)
    return ok, err


def crawl() -> None:
    """メイン処理: 未インポートの最新 pkl ファイルを取得して Supabase に保存。"""
    sb = get_supabase()

    logger.info("Listing elo_results_*.pkl files from HF Space...")
    pkl_files = list_elo_pkl_files()
    if not pkl_files:
        logger.warning("No elo_results_*.pkl files found in HF Space.")
        return
    logger.info(f"Found {len(pkl_files)} pkl files. Latest: {pkl_files[-1][0]}")

    imported = get_imported_dates(sb)
    logger.info(f"Already imported: {len(imported)} snapshot dates")

    # 未インポートのファイルを新しい順に最大 3 件処理
    # （初回実行時の大量インポートを避けるため上限を設ける）
    new_files = [(f, d) for f, d in reversed(pkl_files) if d.isoformat() not in imported][:3]

    if not new_files:
        logger.info("No new elo_results_*.pkl files to import.")
        return

    total_ok = total_err = 0
    for filename, snapshot_date in new_files:
        logger.info(f"Processing {filename} (snapshot_date={snapshot_date}) ...")
        rows = download_and_parse_pkl(filename, snapshot_date)
        if not rows:
            logger.warning(f"  No data extracted from {filename}, skipping.")
            continue
        logger.info(f"  Extracted {len(rows)} model rankings")

        ok, err = upsert_rankings(sb, rows)
        total_ok += ok
        total_err += err
        logger.info(f"  Upserted: ok={ok}, err={err}")

    logger.info(f"Crawl complete: total_ok={total_ok}, total_err={total_err}")

    if total_ok + total_err > 0:
        error_rate = total_err / (total_ok + total_err)
        if error_rate > ERROR_RATE_THRESHOLD:
            logger.error(
                f"Error rate {error_rate:.1%} exceeded {ERROR_RATE_THRESHOLD:.0%} — exiting with code 1"
            )
            sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="LMArena ELO rankings crawler/importer")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--export-json",
        nargs="?",
        const=str(_DEFAULT_EXPORT_PATH),
        help="Parse latest upstream pkl files and write normalized JSON without DB access",
    )
    mode.add_argument(
        "--import-json",
        help="Import normalized JSON into Supabase without loading pickle",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=3,
        help="Max latest pkl files to parse in --export-json mode",
    )
    args = parser.parse_args()

    if args.export_json:
        export_rankings_json(Path(args.export_json), max_files=args.max_files)
    elif args.import_json:
        import_rankings_json(Path(args.import_json))
    else:
        crawl()


if __name__ == "__main__":
    main()
