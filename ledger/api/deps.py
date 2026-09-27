import hmac
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session, sessionmaker

from ledger import errors
from ledger.config import Settings

IDEMPOTENCY_KEY_MAX = 255


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_session_factory(request: Request) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    return factory


def get_session(
    factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> Iterator[Session]:
    with factory() as session:
        yield session


def require_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    if x_api_key is None:
        raise errors.Unauthorized("X-API-Key header is required")
    presented = x_api_key.encode()
    # Compare against every key without short-circuiting to avoid leaking which matched.
    matched = False
    for key in settings.api_key_set:
        matched |= hmac.compare_digest(presented, key.encode())
    if not matched:
        raise errors.Unauthorized("invalid API key")


def idempotency_key(
    idempotency_key: Annotated[
        str,
        Header(
            min_length=1,
            max_length=IDEMPOTENCY_KEY_MAX,
            pattern=r"^[\x21-\x7e]+$",
            description="Client-generated unique key (e.g. a UUID) making the write safe to retry.",
        ),
    ],
) -> str:
    return idempotency_key


SessionDep = Annotated[Session, Depends(get_session)]
SessionFactoryDep = Annotated[sessionmaker[Session], Depends(get_session_factory)]
IdempotencyKeyDep = Annotated[str, Depends(idempotency_key)]
OptionalIdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=IDEMPOTENCY_KEY_MAX,
        pattern=r"^[\x21-\x7e]+$",
    ),
]
SettingsDep = Annotated[Settings, Depends(get_settings)]
