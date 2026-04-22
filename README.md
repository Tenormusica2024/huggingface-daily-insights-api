# HuggingFace Daily Insights API

[![Daily Crawl](https://github.com/Tenormusica2024/huggingface-daily-insights-api/actions/workflows/daily_crawl.yml/badge.svg)](https://github.com/Tenormusica2024/huggingface-daily-insights-api/actions/workflows/daily_crawl.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

An open-source data pipeline that tracks HuggingFace trending models, arXiv AI/ML papers, and LMArena ELO rankings — storing daily snapshots so you can query **historical time-series data** that the upstream sources do not expose.

**Free to self-host. Free to fork. Free to use the daily CSV dumps.**

> ⚠️ **Disclaimer**: This project is **not affiliated with HuggingFace, arXiv, or LMArena**. All data is aggregated from public sources; please respect the terms of the original providers.

---

## Why this exists

The HuggingFace Hub API returns only the **current** state of a model (likes, downloads, tags). No history. This project crawls the Hub daily and stores snapshots so you can answer questions like:

- Which models gained the most likes over the past 7 / 30 / 90 days?
- Which models were published this week?
- How did a specific model's popularity evolve over time?

Similar gaps are filled for arXiv (recent AI/ML papers) and LMArena (ELO rankings over time).

---

## How to use the data

There are three ways to consume the data. Pick whichever fits your use case.

### 1. Daily CSV snapshots (easiest — no API, no setup)

Every day at 00:00 UTC, the GitHub Actions pipeline publishes a new GitHub Release with CSV dumps attached as assets.

- Latest release: <https://github.com/Tenormusica2024/huggingface-daily-insights-api/releases/latest>
- Tag format: `snapshot-YYYY-MM-DD`
- Files: `models.csv`, `model_snapshots.csv` (last 30 days), `papers.csv`, `arena_rankings.csv`

```bash
# Example: fetch the latest snapshot via gh CLI
gh release download -R Tenormusica2024/huggingface-daily-insights-api --pattern '*.csv'
```


### 1.5 Static dashboard (GitHub Pages)

A zero-backend dashboard is available from the `docs/` directory and is designed for GitHub Pages:

- Dashboard: <https://tenormusica2024.github.io/huggingface-daily-insights-api/>
- It fetches the latest GitHub Release metadata and reads the CSV assets client-side.
- No hosted API or server-side secret is required.

If you fork this repo, enable GitHub Pages with the included `Deploy GitHub Pages dashboard` workflow.

### 2. Self-host the API (recommended for integrations)

Fork / clone this repo, provision a Supabase project, and deploy the FastAPI service anywhere that runs a container (Cloud Run, Fly.io, Render, your own VPS). See [Self-hosting](#self-hosting) below.

### 3. Query the database directly (if you enable public read access)

If you configure your Supabase project with a read-only `anon` key and appropriate RLS policies, anyone can run SQL queries against the same database the API uses. This is optional and up to the operator.

A reference RLS policy script is provided at [`sql/migrations/optional/002_public_read_policies.sql`](sql/migrations/optional/002_public_read_policies.sql). It is placed under `optional/` to signal that the default deployment does **not** apply it — operators who want direct DB read access must opt in explicitly.

---

## Endpoints

The self-hosted API exposes the following endpoints. Replace `${BASE_URL}` with your deployment URL.

> **CORS & access model**: This API is a **read-only public service**. The default CORS policy allows `GET` requests from **any origin** (`allow_origins=["*"]`) so that static dashboards and notebooks can call it without a proxy. If you fork and deploy your own instance behind authentication, edit `app.add_middleware(CORSMiddleware, ...)` in `src/api.py` to restrict origins accordingly.

### `GET /health`
Returns service status.
```
GET /health
→ {"status": "ok"}
```

### `GET /models/trending`
Models ranked by likes increase over the past N days.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `pipeline_tag` | string | — | Filter by task type (e.g. `text-generation`) |
| `days` | int | 7 | Lookback window (1–90) |
| `limit` | int | 20 | Max results (1–100) |

```json
[
  {
    "model_id": "Nanbeige/Nanbeige4.1-3B",
    "pipeline_tag": "text-generation",
    "likes_latest": 811,
    "likes_delta": 33,
    "snapshot_date_from": "2026-02-24",
    "snapshot_date_to": "2026-02-27"
  }
]
```

Available pipeline tags: `text-generation`, `text2text-generation`, `image-text-to-text`, `text-to-image`

### `GET /models/new`
Models first seen within the past N days.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `pipeline_tag` | string | — | Filter by task type |
| `days` | int | 7 | Lookback window (1–90) |
| `limit` | int | 20 | Max results (1–100) |

### `GET /models/{model_id}/history`
Daily snapshots for a specific model (likes, downloads, tags over time).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `model_id` | path | required | HuggingFace model ID (e.g. `Qwen/Qwen2.5-7B-Instruct`) |
| `limit` | int | 30 | Max snapshot records (1–180) |

### `GET /papers/recent`
Recently submitted arXiv papers in AI/ML.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `category` | string | — | Filter by arXiv category (e.g. `cs.AI`) |
| `days` | int | 7 | Lookback window (1–90) |
| `limit` | int | 20 | Max results (1–100) |

Available categories: `cs.AI`, `cs.LG`, `cs.CL`, `cs.CV`, `stat.ML`

### `GET /arena/rankings`
LMArena ELO rankings from the latest available snapshot.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | int | 50 | Max results (1–200) |
| `snapshot_date` | string | — | Specific date (YYYY-MM-DD). Defaults to latest. |

> **Note on LMArena data freshness**: The upstream source (`lmarena-ai/lmarena-leaderboard` HF Space) publishes new ELO results irregularly — sometimes months between updates. As a result, this endpoint may serve a snapshot several months old. Check `snapshot_date` in the response.

---

## Architecture

```
GitHub Actions (daily, UTC 00:00 / JST 09:00)
  ├── crawl_hf.py              → HuggingFace Hub API → models / model_snapshots (Supabase)
  ├── crawl_arxiv.py           → arXiv Atom XML API  → papers (Supabase)
  ├── crawl_arena.py           → lmarena-ai HF Space → arena_rankings (Supabase)
  └── export_daily_snapshot.py → Supabase → CSV → GitHub Releases

Container host (Cloud Run / Fly.io / Render / VPS)
  └── FastAPI + uvicorn → Supabase (PostgreSQL)
```

**Data collection scope**:
- HuggingFace: top 200 models per pipeline tag, 4 tags daily
- arXiv: top 100 papers per category, 5 categories daily
- Retention: indefinite (time-series accumulates daily)

---

## Self-hosting

### Prerequisites
- Python 3.11+
- A [Supabase](https://supabase.com) project (free tier is plenty)
- A container host (Cloud Run, Fly.io, Render, etc.) or local uvicorn

### 1. Create the database schema
Run `sql/schema.sql` in the Supabase SQL editor. This creates `models`, `model_snapshots`, `papers`, and `arena_rankings` tables with the required indexes.

### 2. Configure environment variables
```bash
SUPABASE_URL=https://<your-project>.supabase.co
SUPABASE_KEY=<service_role_key>  # needed for crawlers to write
```

For read-only API deployments you can use the `anon` key instead — but you'll need to configure RLS policies to allow SELECT on the relevant tables.

### 3. Run the crawlers (one-off or via GitHub Actions)
```bash
python src/crawl_hf.py
python src/crawl_arxiv.py
python src/crawl_arena.py
```

To enable the daily GitHub Actions pipeline, add `SUPABASE_URL` and `SUPABASE_KEY` as repository secrets.

### 4. Run the API locally
```bash
pip install -r requirements.txt
PYTHONPATH=src uvicorn api:app --reload
```

PowerShell:
```powershell
$env:PYTHONPATH="src"; uvicorn api:app --reload
```

### 5. Deploy the API container
```bash
# Cloud Run (recommended — generous free tier)
gcloud run deploy hf-insights --source . \
  --region asia-northeast1 \
  --allow-unauthenticated \
  --set-env-vars SUPABASE_URL=...,SUPABASE_KEY=...

# Or docker build locally
docker build -t hf-insights .
docker run -p 8080:8080 --env-file .env hf-insights
```

---

## Tech stack

- **Runtime**: Python 3.11
- **Framework**: FastAPI + uvicorn
- **Database**: Supabase (PostgreSQL)
- **Container**: Docker (Cloud Run / Fly.io / Render compatible)
- **CI/CD**: GitHub Actions (daily crawl + Release publishing)

---

## Contributing

Issues and PRs welcome. Noteworthy ideas:

- Adding more pipeline tags (audio, video, etc.)
- Hooking up PapersWithCode SotA flags (`pwc_sota_flag`, `pwc_id` are in the schema but not populated yet)
- LLM-generated business impact scores (`business_score`, `business_summary` columns are reserved for this)
- Alternative LMArena data sources (current source updates irregularly)

---

## License

MIT — see [LICENSE](LICENSE).

Data attribution: HuggingFace model metadata © respective authors / Hugging Face. arXiv paper metadata © respective authors (arXiv.org non-exclusive license). LMArena rankings © LMSYS.
