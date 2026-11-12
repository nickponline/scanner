# Build context: repo root. API + bundled viewer SPA.
FROM node:22-bookworm-slim AS viewer
WORKDIR /viewer
COPY viewer/package.json viewer/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY viewer/ ./
RUN rm -rf public/fixtures && npm run build

FROM python:3.11-slim-bookworm
WORKDIR /app
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api/app ./app
COPY --from=viewer /viewer/dist /app/viewer
ENV PYTHONUNBUFFERED=1 VIEWER_DIR=/app/viewer PORT=8080
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'
