from typing import Any


class LedgerError(Exception):
    status_code: int = 400
    code: str = "ledger_error"
    title: str = "Ledger error"

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


class NotFound(LedgerError):
    status_code = 404
    code = "not_found"
    title = "Resource not found"


class Unprocessable(LedgerError):
    status_code = 422
    code = "unprocessable"
    title = "Request cannot be processed"


class UnknownAccount(Unprocessable):
    code = "unknown_account"
    title = "Referenced account does not exist"


class UnbalancedTransaction(Unprocessable):
    code = "unbalanced_transaction"
    title = "Debits and credits do not balance"


class CurrencyMismatch(Unprocessable):
    code = "currency_mismatch"
    title = "Currencies do not match"


class IncompatibleAccounts(Unprocessable):
    code = "incompatible_accounts"
    title = "Accounts cannot be used together"


class DuplicatePostingAccount(Unprocessable):
    code = "duplicate_posting_account"
    title = "An account appears more than once in a transaction"


class InvalidCursor(Unprocessable):
    code = "invalid_cursor"
    title = "Pagination cursor is invalid"


class InsufficientFunds(LedgerError):
    status_code = 409
    code = "insufficient_funds"
    title = "Insufficient funds"


class Conflict(LedgerError):
    status_code = 409
    code = "conflict"
    title = "Resource conflict"


class AlreadyReversed(Conflict):
    code = "already_reversed"
    title = "Transaction has already been reversed"


class IdempotencyKeyReuse(LedgerError):
    status_code = 422
    code = "idempotency_key_reuse"
    title = "Idempotency key was already used with a different request"


class Unauthorized(LedgerError):
    status_code = 401
    code = "unauthorized"
    title = "Missing or invalid API key"


class ServiceBusy(LedgerError):
    status_code = 503
    code = "contention"
    title = "Write contention, retry the request"
