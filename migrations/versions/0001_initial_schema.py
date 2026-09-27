"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-27 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


APPEND_ONLY_SQL = """
CREATE FUNCTION ledger_forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% on % is forbidden: the ledger is append-only', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END
$$;

CREATE TRIGGER postings_append_only
    BEFORE UPDATE OR DELETE ON postings
    FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();

CREATE TRIGGER transactions_append_only
    BEFORE UPDATE OR DELETE ON transactions
    FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();
"""

BALANCE_CHECK_SQL = """
CREATE FUNCTION ledger_check_transaction_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    posting_count integer;
    offending record;
BEGIN
    IF EXISTS (
        SELECT 1 FROM accounts a WHERE a.id = NEW.account_id AND a.currency <> NEW.currency
    ) THEN
        RAISE EXCEPTION 'posting % currency % does not match its account', NEW.id, NEW.currency
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT count(*) INTO posting_count FROM postings WHERE transaction_id = NEW.transaction_id;
    IF posting_count < 2 THEN
        RAISE EXCEPTION 'transaction % has fewer than two postings', NEW.transaction_id
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT currency, sum(CASE WHEN direction = 'debit' THEN amount ELSE -amount END) AS net
      INTO offending
      FROM postings
     WHERE transaction_id = NEW.transaction_id
     GROUP BY currency
    HAVING sum(CASE WHEN direction = 'debit' THEN amount ELSE -amount END) <> 0
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'transaction % is unbalanced in % by %',
            NEW.transaction_id, offending.currency, offending.net
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER postings_balanced
    AFTER INSERT ON postings
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_check_transaction_balanced();
"""

DROP_TRIGGERS_SQL = """
DROP TRIGGER IF EXISTS postings_balanced ON postings;
DROP TRIGGER IF EXISTS postings_append_only ON postings;
DROP TRIGGER IF EXISTS transactions_append_only ON transactions;
DROP FUNCTION IF EXISTS ledger_check_transaction_balanced();
DROP FUNCTION IF EXISTS ledger_forbid_mutation();
"""


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("allow_negative", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("posted_debits", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("posted_credits", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_accounts_currency_iso")),
        sa.CheckConstraint(
            "type IN ('asset', 'expense', 'liability', 'equity', 'revenue')",
            name=op.f("ck_accounts_type_valid"),
        ),
        sa.CheckConstraint("posted_credits >= 0", name=op.f("ck_accounts_credits_non_negative")),
        sa.CheckConstraint("posted_debits >= 0", name=op.f("ck_accounts_debits_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
        sa.UniqueConstraint("code", name=op.f("uq_accounts_code")),
    )
    op.create_table(
        "idempotency_records",
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("scope", "key", name=op.f("pk_idempotency_records")),
    )
    op.create_table(
        "transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("reverses_id", sa.Uuid(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["reverses_id"],
            ["transactions.id"],
            name=op.f("fk_transactions_reverses_id_transactions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
        sa.UniqueConstraint("reverses_id", name=op.f("uq_transactions_reverses_id")),
    )
    op.create_index("ix_transactions_created_at", "transactions", ["created_at"], unique=False)
    op.create_table(
        "postings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(length=6), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("balance_after", sa.BigInteger(), nullable=False),
        sa.Column("account_version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "direction IN ('debit', 'credit')", name=op.f("ck_postings_direction_valid")
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_postings_amount_positive")),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_postings_account_id_accounts")
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_postings_transaction_id_transactions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_postings")),
    )
    op.create_index("ix_postings_account_id_id", "postings", ["account_id", "id"], unique=False)
    op.create_index("ix_postings_transaction_id", "postings", ["transaction_id"], unique=False)
    op.execute(APPEND_ONLY_SQL)
    op.execute(BALANCE_CHECK_SQL)


def downgrade() -> None:
    op.execute(DROP_TRIGGERS_SQL)
    op.drop_index("ix_postings_transaction_id", table_name="postings")
    op.drop_index("ix_postings_account_id_id", table_name="postings")
    op.drop_table("postings")
    op.drop_index("ix_transactions_created_at", table_name="transactions")
    op.drop_table("transactions")
    op.drop_table("idempotency_records")
    op.drop_table("accounts")
