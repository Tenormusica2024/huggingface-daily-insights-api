"""
AI Model Tracker API
FastAPI application serving trending/new model data from Supabase.

Endpoints:
  GET /models/trending  - Top models by likes growth over N days
  GET /models/new       - Recently first-seen models
  GET /models/{model_id}/history - Snapshot history for a specific model
"""

from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from config import TARGET_PIPELINE_TAGS, LIMIT_PER_TAG
from db import get_supabase

# 1 日 1 タグあたりのスナップショット行数の見積もり上限。
# 実体は crawl_hf.py の LIMIT_PER_TAG で頭打ちされる（クロール側は 200 件で切るため、
# この +50 バッファはクロール側の上限を引き上げる意味はない）。
# 目的は trending クエリの row_cap 計算における安全側の見積もり（pipeline_tag 変動・
# 将来 LIMIT_PER_TAG を引き上げた場合の緩衝）であり、実取得件数には影響しない。
_MODELS_PER_TAG = LIMIT_PER_TAG + 50
# config.py の TARGET_PIPELINE_TAGS から自動計算（手動同期不要）
_N_PIPELINE_TAGS = len(TARGET_PIPELINE_TAGS)
# LIMIT_PER_TAG(200) × N_PIPELINE_TAGS(4) × days(25) 相当の絶対上限
# 上限超えは trending 精度の低下と引き換えに DB 負荷を抑制する
_ROW_CAP_MAX = 20_000

app = FastAPI(
    title="HuggingFace Daily Insights API",
    description="Daily insights on HuggingFace models, arXiv papers, and LMArena rankings with historical time-series data",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/models/trending")
def get_trending(
    pipeline_tag: Optional[str] = Query(None, description="Filter by task type e.g. text-generation"),
    days: int = Query(7, ge=1, le=90, description="Lookback window in days"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
):
    """
    Return models ranked by likes increase over the past N days.
    Requires at least two snapshots (today and N days ago) to compute delta.
    """
    sb = get_supabase()

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    # pipeline_tag 指定なし = 全タグが対象。タグ数分のモデルを見込んで行数上限を設定
    # days × (1タグのモデル数上限) × (対象タグ数) でウィンドウ内の全スナップを取得できる
    tag_multiplier = 1 if pipeline_tag else _N_PIPELINE_TAGS
    row_cap = min(days * _MODELS_PER_TAG * tag_multiplier, _ROW_CAP_MAX)

    query = (
        sb.table("model_snapshots")
        .select("model_id, snapshot_date, likes, pipeline_tag")
        .gte("snapshot_date", cutoff)
        .order("snapshot_date", desc=False)
        .limit(row_cap)
    )
    if pipeline_tag:
        query = query.eq("pipeline_tag", pipeline_tag)

    try:
        resp = query.execute()
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database unavailable") from e
    rows = resp.data

    # model_id ごとにスナップショットをまとめて likes の増分（最新 - 最古）を計算
    model_snapshots: dict[str, list] = defaultdict(list)
    for row in rows:
        model_snapshots[row["model_id"]].append(row)

    deltas = []
    for model_id, snaps in model_snapshots.items():
        if len(snaps) < 2:
            continue
        oldest = snaps[0]
        latest = snaps[-1]
        delta = (latest["likes"] or 0) - (oldest["likes"] or 0)
        deltas.append({
            "model_id": model_id,
            "pipeline_tag": latest.get("pipeline_tag"),
            "likes_latest": latest["likes"],
            "likes_delta": delta,
            "snapshot_date_from": oldest["snapshot_date"],
            "snapshot_date_to": latest["snapshot_date"],
        })

    deltas.sort(key=lambda x: x["likes_delta"], reverse=True)
    return deltas[:limit]


@app.get("/models/new")
def get_new(
    pipeline_tag: Optional[str] = Query(None, description="Filter by task type"),
    days: int = Query(7, ge=1, le=90, description="Lookback window in days"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
):
    """
    Return models first seen within the past N days, sorted by first_seen_at descending.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    sb = get_supabase()
    query = (
        sb.table("models")
        .select("id, name, author, pipeline_tag, first_seen_at")
        .gte("first_seen_at", cutoff)
        .order("first_seen_at", desc=True)
        .limit(limit)
    )
    if pipeline_tag:
        query = query.eq("pipeline_tag", pipeline_tag)

    try:
        resp = query.execute()
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database unavailable") from e
    return resp.data


@app.get("/models/{model_id:path}/history")
def get_history(
    model_id: str,
    limit: int = Query(30, ge=1, le=180, description="Max snapshot records"),
):
    """
    Return time-series snapshots for a specific model.
    model_id uses path param to support 'author/model-name' format.
    """
    sb = get_supabase()
    try:
        resp = (
            sb.table("model_snapshots")
            .select("snapshot_date, downloads_30d, likes, pipeline_tag, tags")
            .eq("model_id", model_id)
            .order("snapshot_date", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database unavailable") from e
    if not resp.data:
        raise HTTPException(status_code=404, detail=f"No snapshots found for model '{model_id}'")
    return resp.data


@app.get("/arena/rankings")
def get_arena_rankings(
    limit: int = Query(50, ge=1, le=200, description="Max results"),
    snapshot_date: Optional[date] = Query(
        None, description="Specific date (YYYY-MM-DD). Defaults to latest."
    ),
):
    """
    Return LMArena ELO rankings.
    Defaults to the latest available snapshot date.
    Source: lmarena-ai/lmarena-leaderboard HF Space (updated irregularly).
    """
    sb = get_supabase()

    # 指定がなければ最新の snapshot_date を使用
    if snapshot_date is None:
        try:
            latest_resp = (
                sb.table("arena_rankings")
                .select("snapshot_date")
                .order("snapshot_date", desc=True)
                .limit(1)
                .execute()
            )
        except Exception as e:
            raise HTTPException(status_code=503, detail="Database unavailable") from e
        if not latest_resp.data:
            raise HTTPException(status_code=404, detail="No arena rankings data available yet")
        snapshot_date_str = latest_resp.data[0]["snapshot_date"]
    else:
        snapshot_date_str = snapshot_date.isoformat()

    try:
        resp = (
            sb.table("arena_rankings")
            .select("snapshot_date, model_name, rank, elo_score")
            .eq("snapshot_date", snapshot_date_str)
            .order("rank", desc=False)
            .limit(limit)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database unavailable") from e
    if not resp.data:
        raise HTTPException(status_code=404, detail=f"No rankings found for {snapshot_date_str}")
    return resp.data


@app.get("/papers/recent")
def get_recent_papers(
    category: Optional[str] = Query(None, description="Filter by arXiv category e.g. cs.AI"),
    days: int = Query(7, ge=1, le=90, description="Lookback window in days"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
):
    """
    Return recently submitted arXiv papers, sorted by submitted_at descending.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    sb = get_supabase()
    query = (
        sb.table("papers")
        .select("arxiv_id, title, authors, submitted_at, category, pwc_sota_flag")
        .gte("submitted_at", cutoff)
        .order("submitted_at", desc=True)
        .limit(limit)
    )
    if category:
        query = query.eq("category", category)

    try:
        resp = query.execute()
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database unavailable") from e
    return resp.data
