import uuid
from datetime import datetime
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ledger.domain import MAX_AMOUNT, AccountType, Direction

T = TypeVar("T")

Amount = Annotated[int, Field(strict=True, gt=0, le=MAX_AMOUNT, description="Minor units")]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$", examples=["INR"])]
Metadata = Annotated[dict[str, str], Field(default_factory=dict, max_length=50)]


def _check_metadata(value: dict[str, str]) -> dict[str, str]:
    for k, v in value.items():
        if not 1 <= len(k) <= 64:
            raise ValueError("metadata keys must be 1-64 characters")
        if len(v) > 500:
            raise ValueError("metadata values must be at most 500 characters")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AccountCreate(StrictModel):
    code: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9:._-]{0,63}$", examples=["wallet:u_42"])]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    type: AccountType
    currency: Currency
    allow_negative: bool = False
    metadata: Metadata

    _metadata = field_validator("metadata")(_check_metadata)


class PostingIn(StrictModel):
    account_id: uuid.UUID
    direction: Direction
    amount: Amount


class TransactionCreate(StrictModel):
    description: Annotated[str | None, Field(max_length=500)] = None
    postings: Annotated[list[PostingIn], Field(min_length=2, max_length=100)]
    metadata: Metadata

    _metadata = field_validator("metadata")(_check_metadata)


class TransferCreate(StrictModel):
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    amount: Amount
    description: Annotated[str | None, Field(max_length=500)] = None
    metadata: Metadata

    _metadata = field_validator("metadata")(_check_metadata)

    @model_validator(mode="after")
    def _distinct_accounts(self) -> "TransferCreate":
        if self.from_account_id == self.to_account_id:
            raise ValueError("from_account_id and to_account_id must differ")
        return self


class ReversalCreate(StrictModel):
    description: Annotated[str | None, Field(max_length=500)] = None


class AccountOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    type: AccountType
    currency: str
    allow_negative: bool
    balance: int
    posted_debits: int
    posted_credits: int
    version: int
    metadata: dict[str, str]
    created_at: datetime


class PostingOut(BaseModel):
    id: int
    account_id: uuid.UUID
    direction: Direction
    amount: int
    currency: str
    balance_after: int


class TransactionOut(BaseModel):
    id: uuid.UUID
    description: str | None
    reverses_id: uuid.UUID | None
    reversed_by_id: uuid.UUID | None
    metadata: dict[str, str]
    created_at: datetime
    postings: list[PostingOut]


class BalanceOut(BaseModel):
    account_id: uuid.UUID
    currency: str
    balance: int
    as_of: datetime | None


class StatementLine(BaseModel):
    posting_id: int
    transaction_id: uuid.UUID
    description: str | None
    direction: Direction
    amount: int
    balance_after: int
    created_at: datetime


class Page(BaseModel, Generic[T]):
    data: list[T]
    next_cursor: str | None


class CurrencyTotals(BaseModel):
    currency: str
    debits: int
    credits: int
    balanced: bool


class IntegrityReport(BaseModel):
    ok: bool
    currencies: list[CurrencyTotals]
    drifted_account_ids: list[uuid.UUID]
    unbalanced_transaction_ids: list[uuid.UUID]


class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    code: str
