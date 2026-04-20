"""
Supabase の全テーブルを CSV に書き出し、GitHub Releases にアップロードするための
ローカルファイルを生成する日次スナップショットエクスポートスクリプト。

出力先: ./snapshots/YYYY-MM-DD/
  - models.csv
  - model_snapshots.csv
  - papers.csv
  - arena_rankings.csv

GitHub Actions ワークフローで呼び出し、生成された CSV 群を snapshot-YYYY-MM-DD
タグの Release にアセットとして添付する。

設計メモ:
- Supabase の PostgREST は 1 リクエスト最大 1000 行なので range ページネーションで全件取得
- model_snapshots は過去 30 日分のみエクスポート（累積するとファイルが肥大化するため）
- 全データのアーカイブが必要なユーザー向けには、毎日の差分を release に残す運用で代替
"""

import csv
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from db import get_supabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Supabase PostgREST の 1 リクエストあたりの最大行数
_PAGE_SIZE = 1000
# model_snapshots は過去 N 日分のみエクスポート（累積肥大化対策）
_SNAPSHOT_LOOKBACK_DAYS = 30


def fetch_all(table: str, columns: str, order_col: str, filters: dict | None = None) -> list[dict]:
    """
    Supabase から指定テーブルの全行を range ページネーションで取得する。
    filters は eq/gte などの演算子と値のタプル辞書（例: {"snapshot_date": ("gte", "2026-01-01")}）。
    """
    sb = get_supabase()
    all_rows: list[dict] = []
    offset = 0
    while True:
        query = sb.table(table).select(columns).order(order_col, desc=False)
        if filters:
            for col, (op, val) in filters.items():
                query = getattr(query, op)(col, val)
        resp = query.range(offset, offset + _PAGE_SIZE - 1).execute()
        rows = resp.data
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return all_rows


def write_csv(rows: list[dict], out_path: Path) -> None:
    """辞書リストを CSV に書き出す。

    rows が空の場合は空ファイルを出力する（DictWriter はサンプル行から fieldnames を推定する都合上、
    事前定義なしではヘッダー行を書けないため）。Release アセットとしては「該当データなし」を
    サイレント成功とせず、WARNING ログで検知可能にする。
    """
    if not rows:
        logger.warning(f"  (empty) {out_path.name}")
        out_path.write_text("", encoding="utf-8")
        return
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"  wrote {out_path.name}: {len(rows)} rows")


def export_all(out_dir: Path) -> None:
    """全テーブルを CSV として出力する。"""
    out_dir.mkdir(parents=True, exist_ok=True)

    # models マスタ（全件）
    logger.info("Exporting models ...")
    models = fetch_all(
        "models",
        "id, name, author, pipeline_tag, first_seen_at, arxiv_id, pwc_id",
        order_col="first_seen_at",
    )
    write_csv(models, out_dir / "models.csv")

    # model_snapshots（過去 N 日分）
    cutoff = (date.today() - timedelta(days=_SNAPSHOT_LOOKBACK_DAYS)).isoformat()
    logger.info(f"Exporting model_snapshots (since {cutoff}) ...")
    snapshots = fetch_all(
        "model_snapshots",
        "model_id, snapshot_date, downloads_30d, likes, pipeline_tag, tags",
        order_col="snapshot_date",
        filters={"snapshot_date": ("gte", cutoff)},
    )
    write_csv(snapshots, out_dir / "model_snapshots.csv")

    # papers（全件）
    logger.info("Exporting papers ...")
    papers = fetch_all(
        "papers",
        "arxiv_id, title, abstract, submitted_at, authors, category, pwc_sota_flag",
        order_col="submitted_at",
    )
    write_csv(papers, out_dir / "papers.csv")

    # arena_rankings（全件）
    logger.info("Exporting arena_rankings ...")
    arena = fetch_all(
        "arena_rankings",
        "snapshot_date, model_name, rank, elo_score",
        order_col="snapshot_date",
    )
    write_csv(arena, out_dir / "arena_rankings.csv")


def main() -> None:
    # GitHub Actions から渡される日付があればそれを使い、なければ今日
    snapshot_date = os.environ.get("SNAPSHOT_DATE", date.today().isoformat())
    out_dir = Path("snapshots") / snapshot_date

    logger.info(f"Exporting daily snapshot to {out_dir}")
    try:
        export_all(out_dir)
    except Exception as e:
        logger.error(f"Export failed: {e}")
        sys.exit(1)
    logger.info("Export complete.")


if __name__ == "__main__":
    main()
