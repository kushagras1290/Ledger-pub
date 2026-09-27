from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ledger import __version__
from ledger.api.deps import SessionDep, require_api_key
from ledger.schemas import IntegrityReport
from ledger.service import LedgerService

router = APIRouter(tags=["system"])


@router.get("/healthz", include_in_schema=False)
def liveness() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/readyz", include_in_schema=False)
def readiness(session: SessionDep) -> JSONResponse:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(status_code=200, content={"status": "ready"})


@router.get(
    "/v1/admin/integrity",
    response_model=IntegrityReport,
    dependencies=[Depends(require_api_key)],
)
def integrity(session: SessionDep) -> IntegrityReport:
    return LedgerService(session).integrity_report()
