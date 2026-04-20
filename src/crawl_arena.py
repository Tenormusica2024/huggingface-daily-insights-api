"""
LMArena ELO ランキング日次クロールスクリプト
毎日 UTC 00:00 に GitHub Actions から実行される

データソース: lmarena-ai/lmarena-leaderboard HF Space
              elo_results_YYYYMMDD.pkl → ELO ランキング
保存先: Supabase (arena_rankings テーブル)

注意: HF Space のデータ更新頻度はまちまちで、数週間単位での更新になる場合がある。
     既存 snapshot_date はスキップするため、重複インポートは発生しない。
"""

import logging
import pickle
import re
import sys
import tempfile
from datetime import date

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


def get_imported_dates(sb: Client) -> set[date]:
    """Supabase に既にインポート済みの snapshot_date セットを返す。"""
    resp = (
        sb.table("arena_rankings")
        .select("snapshot_date")
        .order("snapshot_date", desc=True)
        .limit(500)
        .execute()
    )
    return {row["snapshot_date"] for row in resp.data}


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
            # 上流が侵害された場合は本プロセスで RCE に至るため、運用上は Cloud Run /
            # GitHub Actions の IAM を最小権限に保ち、影響範囲を限定する。
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


if __name__ == "__main__":
    crawl()
