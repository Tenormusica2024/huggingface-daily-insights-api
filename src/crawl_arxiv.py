"""
arXiv 日次クロールスクリプト
毎日 UTC 00:00 に GitHub Actions から実行される

取得対象: cs.AI / cs.LG / cs.CL / cs.CV / stat.ML の最新論文
保存先: Supabase (papers テーブル)
"""

import sys
import time
import logging
import xml.etree.ElementTree as ET

import requests
from supabase import Client

from config import ARXIV_CATEGORIES, PAPERS_PER_CATEGORY, ERROR_RATE_THRESHOLD
from db import get_supabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

ARXIV_API_URL = "http://export.arxiv.org/api/query"

# Atom XML 名前空間
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def fetch_arxiv_papers(category: str, limit: int = PAPERS_PER_CATEGORY) -> list[dict]:
    """
    arXiv API から最新論文を取得する
    ソート: submittedDate 降順（新着順）
    """
    params = {
        "search_query": f"cat:{category}",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": 0,
        "max_results": limit,
    }
    try:
        resp = requests.get(ARXIV_API_URL, params=params, timeout=30)
        resp.raise_for_status()
        return _parse_arxiv_xml(resp.text)
    except requests.RequestException as e:
        logger.error(f"arXiv API fetch failed for {category}: {e}")
        return []
    except ET.ParseError as e:
        logger.error(f"arXiv API returned invalid XML for {category}: {e}")
        return []


def _parse_arxiv_xml(xml_text: str) -> list[dict]:
    """Atom XML レスポンスをパースして論文リストを返す"""
    root = ET.fromstring(xml_text)
    papers = []
    for entry in root.findall("atom:entry", _NS):
        try:
            # http://arxiv.org/abs/2301.00234v1 → 2301.00234（バージョン番号を除去）
            id_url = entry.find("atom:id", _NS).text
            arxiv_id = id_url.split("/abs/")[-1].rsplit("v", 1)[0]

            title = entry.find("atom:title", _NS).text.strip().replace("\n", " ")
            abstract = entry.find("atom:summary", _NS).text.strip().replace("\n", " ")
            submitted_at = entry.find("atom:published", _NS).text

            authors = [
                a.find("atom:name", _NS).text
                for a in entry.findall("atom:author", _NS)
            ]
            papers.append({
                "arxiv_id": arxiv_id,
                "title": title,
                "abstract": abstract,
                "submitted_at": submitted_at,
                "authors": authors,
            })
        except (AttributeError, TypeError) as e:
            # 必須フィールド欠損のエントリは個別スキップ（他エントリへの影響なし）
            logger.warning(f"Skipping malformed entry: {e}")
    return papers


def upsert_paper(sb: Client, paper: dict) -> None:
    """
    papers テーブルに upsert する
    同一 arxiv_id は全フィールドを上書き（category 等の後付けカラムを更新するため）
    pwc_sota_flag は別バッチ（PapersWithCode 連携）で付与
    """
    sb.table("papers").upsert(
        paper,
        on_conflict="arxiv_id",
    ).execute()


def crawl(categories: list[str] = ARXIV_CATEGORIES) -> None:
    """メイン処理: 全カテゴリを走査して Supabase に保存"""
    sb = get_supabase()
    total_papers = 0
    total_errors = 0

    for category in categories:
        logger.info(f"Fetching category={category} ...")
        papers = fetch_arxiv_papers(category)
        logger.info(f"  Got {len(papers)} papers")

        for paper in papers:
            try:
                paper["category"] = category  # クロール元カテゴリを付与
                upsert_paper(sb, paper)
                total_papers += 1
            except Exception as e:
                logger.warning(f"  Failed to upsert {paper.get('arxiv_id')}: {e}")
                total_errors += 1

        # arXiv API へのレート制限対策（利用規約準拠: 3秒間隔）
        time.sleep(3)

    total_processed = total_papers + total_errors
    error_rate = total_errors / total_processed if total_processed > 0 else 0.0
    logger.info(
        f"Crawl complete: papers={total_papers}, errors={total_errors}, "
        f"error_rate={error_rate:.1%}"
    )

    if error_rate > ERROR_RATE_THRESHOLD:
        logger.error(
            f"Error rate {error_rate:.1%} exceeded threshold "
            f"{ERROR_RATE_THRESHOLD:.1%} — exiting with code 1"
        )
        sys.exit(1)


if __name__ == "__main__":
    crawl()
