from typing import Any

from fastapi.responses import JSONResponse

from ledger.idempotency import Outcome


def outcome_response(outcome: Outcome) -> JSONResponse:
    return JSONResponse(
        status_code=outcome.status_code,
        content=outcome.body,
        headers={"Idempotent-Replayed": "true" if outcome.replayed else "false"},
    )


def json_body(model: Any) -> dict[str, Any]:
    body: dict[str, Any] = model.model_dump(mode="json")
    return body
