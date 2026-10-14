# syntax=docker/dockerfile:1
# --- stage 1: build the web UI -------------------------------------------------
FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- stage 2: build a wheel ---------------------------------------------------
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist

# --- stage 3: runtime ---------------------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    VIBEVAULT_DATABASE_URL=sqlite:////data/vibevault.db \
    VIBEVAULT_STATIC_DIR=/app/ui
RUN useradd --system --uid 10001 --user-group --home-dir /nonexistent --shell /usr/sbin/nologin vibevault \
 && mkdir -p /data /app/ui && chown vibevault:vibevault /data && chmod 700 /data
COPY --from=build /dist/*.whl /tmp/
RUN pip install /tmp/*.whl && rm /tmp/*.whl
COPY --from=ui /ui/dist /app/ui
USER vibevault
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/readyz', timeout=4).status==200 else 1)"
# Migrations run automatically on start (VIBEVAULT_AUTO_MIGRATE=true). The server binds to all
# interfaces *inside* the container; docker-compose.yml publishes the port on localhost only.
CMD ["vibevault", "serve", "--host", "0.0.0.0", "--port", "8787"]
