FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM node:22-bookworm-slim AS codex
# Optional practice adapter. Copy only the pinned native CLI into the app.
RUN npm install --prefix /codex-package --ignore-scripts --no-audit --no-fund @openai/codex@0.166.0 \
    && case "$(uname -m)" in \
         x86_64) review_platform=x64; review_target=x86_64-unknown-linux-musl ;; \
         aarch64) review_platform=arm64; review_target=aarch64-unknown-linux-musl ;; \
         *) exit 1 ;; \
       esac \
    && cp "/codex-package/node_modules/@openai/codex-linux-$review_platform/vendor/$review_target/bin/codex" /codex

FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/backend/src
ENV OPENBLAS_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false
WORKDIR /app
COPY backend/requirements.lock backend/requirements-dev.lock /app/backend/
RUN pip install --no-cache-dir -r backend/requirements.lock
COPY backend/ /app/backend/
COPY data/ /app/data/
COPY scripts/check_guidance.py /app/scripts/check_guidance.py
RUN python -m event_review.embeddings --download
COPY --from=frontend /build/dist /app/frontend/dist
COPY --from=codex /codex /usr/local/bin/codex
RUN /usr/local/bin/codex --version \
    && useradd --uid 10001 --create-home review \
    && install -d -m 700 -o review -g review /home/review/.codex

FROM base AS test
RUN pip install --no-cache-dir -r backend/requirements-dev.lock
USER review
CMD ["python", "-m", "pytest", "-c", "backend/pyproject.toml", "backend/tests"]

FROM base AS app
USER review
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "event_review.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

