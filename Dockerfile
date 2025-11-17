FROM ghcr.io/astral-sh/uv:python3.13-alpine AS builder

COPY . /app

WORKDIR /app

RUN uv sync --frozen --no-dev

FROM python:3.13-alpine

COPY --from=builder /app/.venv/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /app /app

WORKDIR /app

CMD ["python", "src/main.py"]