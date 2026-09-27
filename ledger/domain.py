from enum import StrEnum

MAX_AMOUNT = 10**15


class Direction(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"

    @property
    def opposite(self) -> "Direction":
        return Direction.CREDIT if self is Direction.DEBIT else Direction.DEBIT


class AccountType(StrEnum):
    ASSET = "asset"
    EXPENSE = "expense"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"

    @property
    def normal_side(self) -> Direction:
        if self in (AccountType.ASSET, AccountType.EXPENSE):
            return Direction.DEBIT
        return Direction.CREDIT


def normal_balance(account_type: AccountType, debits: int, credits: int) -> int:
    """Balance expressed on the account's normal side: positive means 'has value'."""
    if account_type.normal_side is Direction.DEBIT:
        return debits - credits
    return credits - debits
