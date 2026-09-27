import hashlib
import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ledger import errors
from ledger.models import IdempotencyRecord
from ledger.service import constraint_name

logger = logging.getLogger(__name__)

RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})

Handler = Callable[[Session], tuple[int, dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class Outcome:
    status_code: int
    body: dict[str, Any]
    replayed: bool


def request_fingerprint(scope: str, payload: Any) -> str:
    canonical = json.dumps(
        {"scope": scope, "payload": payload}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def execute_idempotent(
    session_factory: sessionmaker[Session],
    *,
    scope: str,
    key: str,
    payload: Any,
    handler: Handler,
    max_attempts: int = 3,
) -> Outcome:
    """Run ``handler`` at most once per (scope, key), atomically with its side effects.

    The idempotency row is inserted *before* the handler runs, inside the same database
    transaction. A concurrent request carrying the same key blocks on the primary-key
    index until the first one commits (then replays its stored response) or rolls back
    (then executes normally). Failed requests leave no trace, so clients may safely retry
    a rejected request with the same key once the underlying condition is fixed.
    """
    fingerprint = request_fingerprint(scope, payload)

    for attempt in range(1, max_attempts + 1):
        try:
            with session_factory() as session, session.begin():
                existing = session.get(IdempotencyRecord, (scope, key))
                if existing is not None:
                    if existing.request_hash != fingerprint:
                        raise errors.IdempotencyKeyReuse(
                            "this Idempotency-Key was used with a different request body"
                        )
                    assert existing.status_code is not None
                    assert existing.response_body is not None
                    return Outcome(existing.status_code, existing.response_body, replayed=True)

                record = IdempotencyRecord(scope=scope, key=key, request_hash=fingerprint)
                session.add(record)
                session.flush()

                status_code, body = handler(session)
                record.status_code = status_code
                record.response_body = body
            return Outcome(status_code, body, replayed=False)
        except IntegrityError as exc:
            if constraint_name(exc) != "pk_idempotency_records":
                raise
            logger.info("idempotency race on %s/%s, replaying", scope, key)
        except DBAPIError as exc:
            if _sqlstate(exc) not in RETRYABLE_SQLSTATES:
                raise
            logger.warning(
                "retryable conflict %s on %s (attempt %d)", _sqlstate(exc), scope, attempt
            )
            time.sleep(random.uniform(0.005, 0.05) * attempt)  # noqa: S311

    raise errors.ServiceBusy("could not complete the write under contention")
