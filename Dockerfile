FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
COPY copybot.py .
COPY src/ ./src/

RUN pip install --no-cache-dir -e ".[dashboard,analytics]"

RUN mkdir -p data logs
