FROM python:3.13-alpine AS builder

COPY . /app

WORKDIR /app

RUN apk add curl && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    source $HOME/.local/bin/env && \
    uv sync

FROM python:3.13-alpine

COPY --from=builder /root/.local /root/.local
COPY --from=builder /app /app

WORKDIR /app

CMD ["python", "src/main.py"]