# Ledger

A double-entry ledger service in Python. It moves money between accounts with the
guarantees a payments backend needs: every transaction balances, a retried request never
posts twice, concurrent writers never overdraw an account or lose an update, and history
is immutable.

**Stack:** FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Alembic · Pydantic v2 · pytest

[![CI](https://github.com/kushagras1290/Ledger-pub/actions/workflows/ci.yml/badge.svg)](https://github.com/kushagras1290/Ledger-pub/actions/workflows/ci.yml)

---

## Contents

- [Quick start](#quick-start)
- [Domain model](#domain-model)
- [API](#api)
- [How correctness is guaranteed](#how-correctness-is-guaranteed)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Configuration](#configuration)
- [Trade-offs and what I'd do next](#trade-offs-and-what-id-do-next)

---

## Quick start

### Docker

```bash
LEDGER_API_KEYS=dev-key-change-me-please docker compose up --build
# API on http://localhost:8000, interactive docs on http://localhost:8000/docs
```

Compose starts Postgres, runs `alembic upgrade head` as a one-shot `migrate` service, and
only then starts the API. Migrations never run inside app replicas, so they can't race
each other.

### Local

```bash
make install                          # venv + editable install with dev deps
export LEDGER_API_KEYS=dev-key-change-me-please
make run                              # alembic upgrade head + uvicorn --reload
make check                            # ruff + mypy --strict + pytest
```

The tests need a Postgres database, `ledger_test` by default. Set
`LEDGER_TEST_DATABASE_URL` to point somewhere else.

### A two-minute tour

```bash
H=(-H "X-API-Key: dev-key-change-me-please" -H "Content-Type: application/json")

# A bank account (asset) and two customer wallets (liabilities: money we owe customers)
BANK=$(curl -s "${H[@]}" localhost:8000/v1/accounts \
  -d '{"code":"cash:hdfc","name":"HDFC Operating","type":"asset","currency":"INR"}' | jq -r .id)
ALICE=$(curl -s "${H[@]}" localhost:8000/v1/accounts \
  -d '{"code":"wallet:alice","name":"Alice","type":"liability","currency":"INR"}' | jq -r .id)
BOB=$(curl -s "${H[@]}" localhost:8000/v1/accounts \
  -d '{"code":"wallet:bob","name":"Bob","type":"liability","currency":"INR"}' | jq -r .id)

# Alice tops up ₹500.00: money lands in the bank (debit asset), we now owe Alice (credit liability)
curl -s "${H[@]}" -H "Idempotency-Key: topup-001" localhost:8000/v1/transactions -d "{
  \"description\": \"UPI top-up\",
  \"postings\": [
    {\"account_id\": \"$BANK\",  \"direction\": \"debit\",  \"amount\": 50000},
    {\"account_id\": \"$ALICE\", \"direction\": \"credit\", \"amount\": 50000}
  ]}"

# Alice pays Bob ₹120.00
curl -s "${H[@]}" -H "Idempotency-Key: p2p-001" localhost:8000/v1/transfers \
  -d "{\"from_account_id\": \"$ALICE\", \"to_account_id\": \"$BOB\", \"amount\": 12000}"

curl -s "${H[@]}" localhost:8000/v1/accounts/$ALICE/statement | jq
curl -s "${H[@]}" localhost:8000/v1/admin/integrity | jq
```

---

## Domain model

```
accounts ─────────┐            transactions (append-only)
  id, code         │              id, description, metadata
  type, currency   │              reverses_id ──► transactions.id (unique)
  posted_debits    │              created_at
  posted_credits   │                   │ 1
  version          │                   │
  allow_negative   │                   ▼ n (≥ 2)
                   └──────────► postings (append-only)
                                  transaction_id, account_id
                                  direction (debit|credit), amount > 0
                                  currency, balance_after, account_version

idempotency_records (scope, key) → request_hash, status_code, response_body
```

| Concept | Rule |
|---|---|
| **Amount** | Integer minor units (paise, cents), `0 < amount ≤ 10^15`. Floats and numeric strings are rejected, not coerced. |
| **Account type** | `asset` and `expense` are *debit-normal*; `liability`, `equity` and `revenue` are *credit-normal*. |
| **Balance** | Reported on the account's normal side: `debits − credits` for debit-normal accounts, `credits − debits` otherwise. Positive means the account holds value. |
| **Transaction** | Two or more postings. For **each currency**, total debits equal total credits. One account per transaction. |
| **Overdraft** | A posting may not take a normal-side balance below zero unless the account has `allow_negative` (e.g. an equity or settlement account). |
| **Immutability** | Postings and transactions are never updated or deleted. You fix a mistake by posting a *reversal*. |

Account balances are stored as running `posted_debits` / `posted_credits` totals, so a
current balance is an O(1) read. Each posting also records `balance_after`, so a
statement shows the running balance with no window functions.

---

## API

All `/v1` routes need `X-API-Key`. Errors are [RFC 7807](https://www.rfc-editor.org/rfc/rfc7807)
`application/problem+json` with a stable, machine-readable `code`.

| Method | Path | Idempotency-Key | Purpose |
|---|---|---|---|
| `POST` | `/v1/accounts` | optional | Create an account |
| `GET` | `/v1/accounts` | | List accounts (keyset pagination by `code`) |
| `GET` | `/v1/accounts/{id}` | | Fetch an account and its current balance |
| `GET` | `/v1/accounts/{id}/balance?as_of=` | | Current or point-in-time balance |
| `GET` | `/v1/accounts/{id}/statement` | | Postings, newest first, with running balance |
| `POST` | `/v1/transactions` | **required** | Post an arbitrary balanced transaction |
| `POST` | `/v1/transfers` | **required** | Move value between two same-side, same-currency accounts |
| `GET` | `/v1/transactions/{id}` | | Fetch a transaction, its postings and reversal link |
| `POST` | `/v1/transactions/{id}/reversal` | **required** | Post the mirror-image transaction |
| `GET` | `/v1/admin/integrity` | | Reconcile the whole ledger (see below) |
| `GET` | `/healthz`, `/readyz` | | Liveness; readiness (checks the DB) |

Every idempotent response carries `Idempotent-Replayed: true|false`. Every response
carries `X-Request-ID`, echoed from the request when well-formed and generated otherwise.

### Error codes

| HTTP | `code` | When |
|---|---|---|
| 401 | `unauthorized` | Missing or wrong API key |
| 404 | `not_found` | Account or transaction in the URL doesn't exist |
| 409 | `insufficient_funds` | Posting would overdraw a non-negative account (includes `available` and `requested`) |
| 409 | `already_reversed` / `conflict` | Double reversal, reversing a reversal, duplicate account code |
| 422 | `unbalanced_transaction` | Debits ≠ credits (includes per-currency `imbalance`) |
| 422 | `unknown_account` / `duplicate_posting_account` | Bad posting references |
| 422 | `currency_mismatch` / `incompatible_accounts` | Transfer between different currencies or different normal sides |
| 422 | `idempotency_key_reuse` | Same key, different request body |
| 422 | `validation_error` / `invalid_cursor` | Malformed input |
| 503 | `contention` | Retry budget exhausted on serialization failures or deadlocks |

---

## How correctness is guaranteed

### 1. Concurrency: pessimistic row locks in a deterministic order

```python
select(Account)
    .where(Account.id.in_(account_ids))
    .order_by(Account.id)                       # global lock order → no deadlock cycles
    .with_for_update()                          # serialize writers per account
    .execution_options(populate_existing=True)  # never trust the identity map after locking
```

Each write locks every account it touches, reads the fresh totals, checks the overdraft
rule, then appends postings and bumps the totals, all in one Postgres transaction under
`READ COMMITTED`. Two writers that share an account queue on its row lock. Writers with
disjoint accounts run fully in parallel. Because every writer takes locks in primary-key
order, A→B and B→A transfers running at the same time can't deadlock.

**A bug the test suite caught.** `transfer()` first loads both accounts to check their
type and currency. The later `SELECT … FOR UPDATE` did lock the rows, but SQLAlchemy
returned the *already-cached* objects with their pre-lock balances, so concurrent
transfers silently lost updates. `populate_existing=True` makes the locked read overwrite
the cache. `tests/test_concurrency.py` fails without it.

Serialization failures (`40001`) and deadlocks (`40P01`) are retried with jittered
backoff up to `LEDGER_MAX_WRITE_RETRIES` times, then surfaced as `503 contention`.

### 2. Idempotency: the key is claimed inside the same transaction as the write

```
BEGIN
  SELECT idempotency_records WHERE (scope, key)
    ├─ found, same request hash      → return stored response  (Idempotent-Replayed: true)
    ├─ found, different request hash → 422 idempotency_key_reuse
    └─ not found:
         INSERT idempotency_records (scope, key, hash)   ← claims the key
         ... lock accounts, validate, post ...
         UPDATE idempotency_records SET status, body
COMMIT
```

Claiming the key *first* is the important part. A concurrent duplicate blocks on the
primary-key index until the first request finishes:

- **First request commits.** The duplicate gets a unique violation, retries, finds the
  stored response and replays it.
- **First request rolls back** (e.g. insufficient funds). The duplicate's insert
  succeeds and it runs normally.

Because the key and the money movement commit or roll back together, the outcomes
"money moved but key not recorded" and "key recorded but money not moved" can't happen.
Failed requests leave nothing behind, so a client can safely retry a rejected payment
with the same key once the underlying problem is fixed. The request hash is a SHA-256 of
canonical JSON of the scope and body. Keys are scoped per operation, so reusing a key
across `/transactions` and `/transfers` doesn't collide.

### 3. Defence in depth: the database enforces the invariants too

The service layer validates everything, but the schema doesn't trust it
(`migrations/versions/0001_initial_schema.py`):

- **`CONSTRAINT TRIGGER … DEFERRABLE INITIALLY DEFERRED`** on `postings` runs at
  `COMMIT` and rejects any transaction that has fewer than two postings, doesn't balance
  per currency, or has a posting whose currency differs from its account's. Because it
  is deferred, a multi-row transaction is checked once it is complete, not row by row.
- **Append-only triggers** raise on any `UPDATE` or `DELETE` of `postings` or
  `transactions`.
- **CHECK constraints:** `amount > 0`, valid direction, ISO-4217-shaped currency, valid
  account type, non-negative running totals.
- **`UNIQUE (reverses_id)`:** a transaction can be reversed at most once, even if two
  reversal requests race.

`tests/test_invariants.py` writes directly through the ORM, bypassing the service, and
shows that each of these rules still holds.

### 4. Reconciliation

`GET /v1/admin/integrity` recomputes the ledger from raw postings and reports:

- per-currency total debits vs credits (must be equal, since the whole ledger balances)
- accounts whose cached `posted_debits`, `posted_credits` or `version` drifted from the
  postings
- any transaction that doesn't balance per currency

In production this would run as a scheduled job that alerts on `ok: false`.

### 5. Reversals, not edits

`POST /transactions/{id}/reversal` locks the original row, refuses if it is already
reversed or is itself a reversal, and posts the mirror-image transaction linked by
`reverses_id`. Reversals obey the same overdraft rule. If Alice already spent her top-up,
reversing it fails with `insufficient_funds` rather than silently pushing her negative.

---

## Project layout

```
ledger/
  domain.py          Account types, normal sides, balance maths (pure, no I/O)
  models.py          SQLAlchemy 2 typed models + naming convention for stable constraint names
  schemas.py         Pydantic request/response contracts (strict ints, extra="forbid")
  service.py         LedgerService: every mutation and query; callers own the transaction
  idempotency.py     execute_idempotent(): claim key → run handler → store response, retry loop
  errors.py          Domain exception hierarchy → HTTP status + problem code
  api/               Thin FastAPI routers; no business logic
  main.py            App factory, problem+json handlers, request-ID middleware
  logging_setup.py   JSON logs with request_id from a contextvar
  config.py          Pydantic settings (LEDGER_* env vars), validated at startup
migrations/          Alembic; 0001 creates tables, constraints and triggers
tests/               38 integration tests against real Postgres
```

Routers parse input and shape output. `LedgerService` owns the rules.
`execute_idempotent` owns the transaction boundary for writes. That split keeps the
service testable on a bare `Session` and lets the same logic run from a queue consumer or
CLI without HTTP.

---

## Testing

```bash
make check    # ruff check, ruff format --check, mypy --strict, pytest
```

The tests run against a real PostgreSQL database, not SQLite or mocks, because the
properties that matter (row locks, deferred triggers, unique-index blocking) only exist
there. The schema is built with the actual Alembic migrations, so the triggers are
tested too.

| File | Covers |
|---|---|
| `test_accounts.py` | Creation, validation, duplicate codes, keyset pagination, auth, health, request IDs |
| `test_transactions.py` | Balancing, multi-currency, strict amounts, overdraft atomicity, transfers, reversals, statements, point-in-time balance |
| `test_idempotency.py` | Replay, key reuse, failed requests not consuming keys, scoping, **32 concurrent duplicates → exactly one posting** |
| `test_concurrency.py` | **40 racing transfers against funds for 20 → exactly 20 succeed, balance ends at 0**; **opposing transfers across 4 wallets → no deadlocks, balances conserved** |
| `test_invariants.py` | DB rejects unbalanced, single-legged and cross-currency writes and any update/delete; integrity report detects tampering |

CI (`.github/workflows/ci.yml`) runs lint, type-check and tests against a Postgres
service container, verifies `alembic check` (migrations match models) and builds the
Docker image.

---

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `LEDGER_DATABASE_URL` | `postgresql+psycopg://ledger:ledger@localhost:5432/ledger` | |
| `LEDGER_API_KEYS` | *(required)* | Comma-separated; each ≥ 16 chars. Several keys allow zero-downtime rotation. Compared in constant time. |
| `LEDGER_DB_POOL_SIZE` / `LEDGER_DB_MAX_OVERFLOW` | `10` / `20` | |
| `LEDGER_DB_STATEMENT_TIMEOUT_MS` | `5000` | Server-side cap so a stuck lock can't pin a connection forever |
| `LEDGER_MAX_WRITE_RETRIES` | `3` | Attempts on `40001` / `40P01` / idempotency races |
| `LEDGER_LOG_LEVEL` | `INFO` | JSON logs to stdout |

---

## Trade-offs and what I'd do next

- **Hot accounts.** Row locks serialize every write to one account. That's correct and
  simple, but a single house account (e.g. fees revenue) becomes a bottleneck at high
  TPS. Fixes, in order of effort: split the hot account into N shard sub-accounts and sum
  them on read; or move hot-account postings to an async, batched applier.
- **Point-in-time balances** sum postings up to `as_of`. That's fine for statements, but
  for large accounts I'd add periodic balance snapshots and sum only the delta since the
  nearest one. The timestamp is `clock_timestamp()` at insert, so a query exactly at
  "now" can miss a transaction that is still committing. Historical queries are stable.
- **Idempotency records** are kept forever. Production would add a TTL (Stripe uses
  24h) and a sweeper, and would scope keys per API client.
- **Multi-tenancy and authorization** are out of scope: one flat API-key set with full
  access. Next steps would be per-client keys, per-account permissions and an audit log
  of who posted what.
- **Pending/two-phase transfers** (authorize → capture/void) would add a `pending`
  balance column and a status on transactions. The locking and idempotency machinery
  stays the same.
- **Outbox events.** Emitting `transaction.posted` through a transactional outbox table
  would let downstream systems consume ledger changes reliably.
- **Observability.** Prometheus metrics for lock wait time, retry counts and replay rate,
  plus OpenTelemetry tracing.
