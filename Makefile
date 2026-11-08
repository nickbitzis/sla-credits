.PHONY: install test lint fmt run month compare totals export gate golden summary screenshot docker clean

DB ?= data/slacredits.db
PORT ?= 8874

install:
	uv sync --python 3.12

test:
	uv run pytest -q

lint:
	uvx ruff check .

fmt:
	uvx ruff check --fix .

run:
	uv run slacredits --db $(DB) api --port $(PORT)

month:
	uv run slacredits month c196 2026-03

compare:
	uv run slacredits compare 2026-03 --component api

totals:
	uv run slacredits --db $(DB) totals

export:
	uv run slacredits --db $(DB) export 2026-03 --format csv

gate:
	uv run slacredits gate --golden golden/credits-2026-03.json

golden:
	uv run slacredits golden --out golden/credits-2026-03.json

summary:
	uv run slacredits --db $(DB) summary --out docs/summary.json

screenshot:
	uv run python scripts/screenshot.py --port $(PORT)

docker:
	docker build -t sla-credits:local .

clean:
	rm -rf data .pytest_cache .ruff_cache
