# Cloud Run / 任意の OCI ランタイム向け Dockerfile
# ローカル: docker build -t hf-insights . && docker run -p 8080:8080 --env-file .env hf-insights
# Cloud Run: gcloud run deploy hf-insights --source . --region asia-northeast1 --allow-unauthenticated

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 依存関係を先にインストール（Docker レイヤーキャッシュ最適化）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# アプリ本体
COPY src ./src
COPY sql ./sql

# 非rootユーザーで実行（コンテナセキュリティのベストプラクティス）
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

# Cloud Run は $PORT を注入する（デフォルト 8080）
ENV PORT=8080
ENV PYTHONPATH=/app/src
EXPOSE 8080

# uvicorn を直接起動（1ワーカー。Cloud Run は水平スケール前提）
CMD exec uvicorn api:app --host 0.0.0.0 --port ${PORT}
