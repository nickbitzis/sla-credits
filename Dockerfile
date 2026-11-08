# One image. The command picks the job:
#   docker run sla-credits api | month c196 2026-03 | totals | export 2026-03 | gate
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
COPY --from=builder --chown=app /app/.venv /app/.venv
COPY --chown=app golden /app/golden
# The SQLite workflow database (claims, disputes, audit log) lives under /app/data.
RUN mkdir -p /app/data && chown app:app /app/data
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SLACREDITS_DB_PATH=/app/data/slacredits.db
USER app
WORKDIR /app
EXPOSE 8874
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD ["slacredits", "healthcheck", "--url", "http://127.0.0.1:8874/healthz"]
ENTRYPOINT ["slacredits"]
CMD ["api", "--host", "0.0.0.0", "--port", "8874"]
