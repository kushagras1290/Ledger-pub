import base64
import binascii
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ledger import errors
from ledger.domain import Direction, normal_balance
from ledger.models import Account, Posting, Transaction
from ledger.schemas import (
    AccountCreate,
    AccountOut,
    BalanceOut,
    CurrencyTotals,
    IntegrityReport,
    Page,
    PostingOut,
    StatementLine,
    TransactionOut,
)


@dataclass(frozen=True, slots=True)
class PostingSpec:
    account_id: uuid.UUID
    direction: Direction
    amount: int


def constraint_name(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> str:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return base64.b64decode(padded, altchars=b"-_", validate=True).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise errors.InvalidCursor("cursor could not be decoded") from exc


def account_out(account: Account) -> AccountOut:
    return AccountOut(
        id=account.id,
        code=account.code,
        name=account.name,
        type=account.account_type,
        currency=account.currency,
        allow_negative=account.allow_negative,
        balance=account.balance,
        posted_debits=account.posted_debits,
        posted_credits=account.posted_credits,
        version=account.version,
        metadata=account.metadata_,
        created_at=account.created_at,
    )


def transaction_out(txn: Transaction) -> TransactionOut:
    return TransactionOut(
        id=txn.id,
        description=txn.description,
        reverses_id=txn.reverses_id,
        reversed_by_id=txn.reversed_by.id if txn.reversed_by is not None else None,
        metadata=txn.metadata_,
        created_at=txn.created_at,
        postings=[
            PostingOut(
                id=p.id,
                account_id=p.account_id,
                direction=Direction(p.direction),
                amount=p.amount,
                currency=p.currency,
                balance_after=p.balance_after,
            )
            for p in txn.postings
        ],
    )


class LedgerService:
    """All ledger mutations. Callers own the database transaction boundary."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_account(self, data: AccountCreate) -> Account:
        account = Account(
            code=data.code,
            name=data.name,
            type=data.type.value,
            currency=data.currency,
            allow_negative=data.allow_negative,
            metadata_=data.metadata,
        )
        try:
            with self.session.begin_nested():
                self.session.add(account)
                self.session.flush()
        except IntegrityError as exc:
            if constraint_name(exc) == "uq_accounts_code":
                raise errors.Conflict(
                    f"account code '{data.code}' already exists", code_value=data.code
                ) from exc
            raise
        self.session.refresh(account)
        return account

    def get_account(self, account_id: uuid.UUID) -> Account:
        account = self.session.get(Account, account_id)
        if account is None:
            raise errors.NotFound(f"account {account_id} not found")
        return account

    def list_accounts(self, limit: int, cursor: str | None) -> Page[AccountOut]:
        stmt = select(Account).order_by(Account.code).limit(limit + 1)
        if cursor is not None:
            stmt = stmt.where(Account.code > decode_cursor(cursor))
        rows = list(self.session.scalars(stmt))
        has_more = len(rows) > limit
        rows = rows[:limit]
        return Page[AccountOut](
            data=[account_out(a) for a in rows],
            next_cursor=encode_cursor(rows[-1].code) if has_more else None,
        )

    def get_transaction(self, txn_id: uuid.UUID) -> Transaction:
        txn = self.session.get(Transaction, txn_id)
        if txn is None:
            raise errors.NotFound(f"transaction {txn_id} not found")
        return txn

    def post_transaction(
        self,
        postings: list[PostingSpec],
        description: str | None = None,
        metadata: dict[str, str] | None = None,
        reverses_id: uuid.UUID | None = None,
    ) -> Transaction:
        if len(postings) < 2:
            raise errors.UnbalancedTransaction("a transaction needs at least two postings")

        account_ids = [p.account_id for p in postings]
        if len(set(account_ids)) != len(account_ids):
            raise errors.DuplicatePostingAccount("each account may appear only once")

        # Lock in a deterministic (primary key) order so concurrent writers touching
        # overlapping account sets can never deadlock on each other. populate_existing
        # forces the locked row values over anything already cached in the identity map;
        # without it a previously loaded Account would keep its stale, pre-lock balance.
        locked = self.session.scalars(
            select(Account)
            .where(Account.id.in_(account_ids))
            .order_by(Account.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        accounts = {a.id: a for a in locked}
        missing = [str(a) for a in account_ids if a not in accounts]
        if missing:
            raise errors.UnknownAccount(f"unknown account(s): {', '.join(missing)}", ids=missing)

        net: defaultdict[str, int] = defaultdict(int)
        for p in postings:
            sign = 1 if p.direction is Direction.DEBIT else -1
            net[accounts[p.account_id].currency] += sign * p.amount
        imbalance = {cur: delta for cur, delta in net.items() if delta != 0}
        if imbalance:
            raise errors.UnbalancedTransaction(
                "debits must equal credits per currency "
                + ", ".join(f"{c}: {d:+d}" for c, d in sorted(imbalance.items())),
                imbalance=imbalance,
            )

        txn = Transaction(
            id=uuid.uuid4(),
            description=description,
            metadata_=metadata or {},
            reverses_id=reverses_id,
        )
        self.session.add(txn)

        for p in postings:
            account = accounts[p.account_id]
            debits = account.posted_debits + (p.amount if p.direction is Direction.DEBIT else 0)
            credits = account.posted_credits + (p.amount if p.direction is Direction.CREDIT else 0)
            new_balance = normal_balance(account.account_type, debits, credits)
            if new_balance < 0 and not account.allow_negative:
                raise errors.InsufficientFunds(
                    f"account {account.id} has {account.balance} available, "
                    f"posting requires {p.amount}",
                    account_id=str(account.id),
                    available=account.balance,
                    requested=p.amount,
                )
            account.posted_debits = debits
            account.posted_credits = credits
            account.version += 1
            self.session.add(
                Posting(
                    transaction_id=txn.id,
                    account_id=account.id,
                    direction=p.direction.value,
                    amount=p.amount,
                    currency=account.currency,
                    balance_after=new_balance,
                    account_version=account.version,
                )
            )

        try:
            self.session.flush()
        except IntegrityError as exc:
            if constraint_name(exc) == "uq_transactions_reverses_id":
                raise errors.AlreadyReversed(f"transaction {reverses_id} already reversed") from exc
            raise
        self.session.refresh(txn)
        return txn

    def transfer(
        self,
        from_account_id: uuid.UUID,
        to_account_id: uuid.UUID,
        amount: int,
        description: str | None,
        metadata: dict[str, str],
    ) -> Transaction:
        source = self.session.get(Account, from_account_id)
        target = self.session.get(Account, to_account_id)
        if source is None or target is None:
            missing = [
                str(i) for i, a in ((from_account_id, source), (to_account_id, target)) if a is None
            ]
            raise errors.UnknownAccount(f"unknown account(s): {', '.join(missing)}", ids=missing)
        if source.currency != target.currency:
            raise errors.CurrencyMismatch(
                f"cannot transfer {source.currency} to {target.currency} account"
            )
        side = source.account_type.normal_side
        if side is not target.account_type.normal_side:
            raise errors.IncompatibleAccounts(
                f"transfer requires accounts with the same normal side; "
                f"{source.type} and {target.type} differ, post an explicit transaction instead"
            )
        return self.post_transaction(
            [
                PostingSpec(source.id, side.opposite, amount),
                PostingSpec(target.id, side, amount),
            ],
            description=description,
            metadata=metadata,
        )

    def reverse_transaction(self, txn_id: uuid.UUID, description: str | None) -> Transaction:
        original = self.session.scalars(
            select(Transaction)
            .where(Transaction.id == txn_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if original is None:
            raise errors.NotFound(f"transaction {txn_id} not found")
        if original.reverses_id is not None:
            raise errors.Conflict("a reversal cannot itself be reversed; post a new transaction")
        if original.reversed_by is not None:
            raise errors.AlreadyReversed(
                f"transaction {txn_id} already reversed by {original.reversed_by.id}",
                reversed_by_id=str(original.reversed_by.id),
            )
        return self.post_transaction(
            [
                PostingSpec(p.account_id, Direction(p.direction).opposite, p.amount)
                for p in original.postings
            ],
            description=description or f"Reversal of {original.id}",
            metadata={"reversal_of": str(original.id)},
            reverses_id=original.id,
        )

    def balance(self, account_id: uuid.UUID, as_of: datetime | None) -> BalanceOut:
        account = self.get_account(account_id)
        if as_of is None:
            return BalanceOut(
                account_id=account.id,
                currency=account.currency,
                balance=account.balance,
                as_of=None,
            )
        debit_sum = func.coalesce(
            func.sum(case((Posting.direction == Direction.DEBIT.value, Posting.amount), else_=0)), 0
        )
        credit_sum = func.coalesce(
            func.sum(case((Posting.direction == Direction.CREDIT.value, Posting.amount), else_=0)),
            0,
        )
        debits, credits = self.session.execute(
            select(debit_sum, credit_sum)
            .join(Transaction, Transaction.id == Posting.transaction_id)
            .where(Posting.account_id == account_id, Transaction.created_at <= as_of)
        ).one()
        return BalanceOut(
            account_id=account.id,
            currency=account.currency,
            balance=normal_balance(account.account_type, int(debits), int(credits)),
            as_of=as_of,
        )

    def statement(
        self, account_id: uuid.UUID, limit: int, cursor: str | None
    ) -> Page[StatementLine]:
        self.get_account(account_id)
        stmt = (
            select(Posting, Transaction.description, Transaction.created_at)
            .join(Transaction, Transaction.id == Posting.transaction_id)
            .where(Posting.account_id == account_id)
            .order_by(Posting.id.desc())
            .limit(limit + 1)
        )
        if cursor is not None:
            raw = decode_cursor(cursor)
            if not raw.isdigit():
                raise errors.InvalidCursor("cursor is not a statement cursor")
            stmt = stmt.where(Posting.id < int(raw))
        rows = self.session.execute(stmt).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        return Page[StatementLine](
            data=[
                StatementLine(
                    posting_id=p.id,
                    transaction_id=p.transaction_id,
                    description=desc,
                    direction=Direction(p.direction),
                    amount=p.amount,
                    balance_after=p.balance_after,
                    created_at=created_at,
                )
                for p, desc, created_at in rows
            ],
            next_cursor=encode_cursor(str(rows[-1][0].id)) if has_more else None,
        )

    def integrity_report(self) -> IntegrityReport:
        debit_sum = func.coalesce(
            func.sum(case((Posting.direction == Direction.DEBIT.value, Posting.amount), else_=0)), 0
        )
        credit_sum = func.coalesce(
            func.sum(case((Posting.direction == Direction.CREDIT.value, Posting.amount), else_=0)),
            0,
        )

        currencies = [
            CurrencyTotals(currency=cur, debits=int(d), credits=int(c), balanced=int(d) == int(c))
            for cur, d, c in self.session.execute(
                select(Posting.currency, debit_sum, credit_sum)
                .group_by(Posting.currency)
                .order_by(Posting.currency)
            ).all()
        ]

        per_account = (
            select(
                Posting.account_id.label("account_id"),
                debit_sum.label("debits"),
                credit_sum.label("credits"),
                func.count().label("n"),
            )
            .group_by(Posting.account_id)
            .subquery()
        )
        drifted = list(
            self.session.scalars(
                select(Account.id)
                .outerjoin(per_account, per_account.c.account_id == Account.id)
                .where(
                    (Account.posted_debits != func.coalesce(per_account.c.debits, 0))
                    | (Account.posted_credits != func.coalesce(per_account.c.credits, 0))
                    | (Account.version != func.coalesce(per_account.c.n, 0))
                )
                .order_by(Account.id)
            )
        )

        unbalanced = list(
            self.session.scalars(
                select(Posting.transaction_id)
                .group_by(Posting.transaction_id, Posting.currency)
                .having(debit_sum != credit_sum)
                .distinct()
            )
        )

        return IntegrityReport(
            ok=all(c.balanced for c in currencies) and not drifted and not unbalanced,
            currencies=currencies,
            drifted_account_ids=drifted,
            unbalanced_transaction_ids=unbalanced,
        )
