FROM python:3.11-slim

WORKDIR /app

# Build deps para web3 (eth-hash necesita compilador C)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY copybot.py .
COPY src/ ./src/
COPY scripts/ ./scripts/

RUN pip install --no-cache-dir -e ".[dashboard,analytics,live,postgres]"

RUN mkdir -p data logs

# Build metadata para /api/admin/version (qué commit corre en runtime)
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
ENV GIT_SHA=${GIT_SHA}
ENV BUILD_TIME=${BUILD_TIME}
