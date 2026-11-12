FROM node:24-alpine AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml uv.lock ./
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" uv==0.12.19
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH="/app/.venv/bin:$PATH"

COPY . .
COPY --from=frontend /frontend/dist ./frontend/dist

RUN groupadd --gid 10001 opspilot && useradd --uid 10001 --gid opspilot --create-home opspilot \
    && mkdir -p /app/data && chown -R opspilot:opspilot /app/data
USER 10001:10001

EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --start-period=60s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=4)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
