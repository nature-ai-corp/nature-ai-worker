FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NATURE_WORKER_HOST=0.0.0.0 \
    NATURE_WORKER_PORT=8080 \
    NATURE_WORKER_DATA_DIR=/data \
    NATURE_WORKER_WORKSPACE=/workspace

WORKDIR /app
COPY requirements.lock ./
RUN pip install -r requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-deps . \
 && useradd --create-home --uid 1000 worker \
 && mkdir -p /data /workspace \
 && chown worker:worker /data /workspace

USER worker
EXPOSE 8080
VOLUME ["/data", "/workspace"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health')"
CMD ["nature-worker"]
