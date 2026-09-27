PY ?= .venv/bin/python
export LEDGER_DATABASE_URL ?= postgresql+psycopg://ledger:ledger@localhost:5432/ledger

.PHONY: install lint typecheck test check migrate run up down

install:
	python3 -m venv .venv && $(PY) -m pip install -e ".[dev]"

lint:
	$(PY) -m ruff check . && $(PY) -m ruff format --check .

typecheck:
	$(PY) -m mypy ledger

test:
	$(PY) -m pytest

check: lint typecheck test

migrate:
	$(PY) -m alembic upgrade head

run: migrate
	$(PY) -m uvicorn ledger.main:create_app --factory --reload

up:
	docker compose up --build -d

down:
	docker compose down -v
