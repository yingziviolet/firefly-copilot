"""初始迁移:建 rules / review_items / audit_logs / ingested_transactions 四张表。

Revision ID: db682d6e3a8e
Revises:
Create Date: 2026-07-26

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "db682d6e3a8e"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# CURRENT_TIMESTAMP 在 PostgreSQL 与 SQLite 上均可用,对应模型里的 func.now()
_NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_logs_event"), "audit_logs", ["event"], unique=False)
    op.create_index(op.f("ix_audit_logs_trace_id"), "audit_logs", ["trace_id"], unique=False)

    op.create_table(
        "ingested_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "RECEIVED",
                "IMPORTED",
                "DUPLICATE",
                "PENDING_REVIEW",
                "REJECTED",
                "FAILED",
                name="ingeststatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("firefly_transaction_id", sa.String(length=32), nullable=True),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_ingested_transactions_fingerprint"),
        "ingested_transactions",
        ["fingerprint"],
        unique=True,
    )
    op.create_index(
        op.f("ix_ingested_transactions_trace_id"),
        "ingested_transactions",
        ["trace_id"],
        unique=False,
    )

    op.create_table(
        "review_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("txn_payload", sa.JSON(), nullable=False),
        sa.Column("suggested_category", sa.String(length=100), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "APPROVED",
                "CORRECTED",
                "REJECTED",
                name="reviewstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("corrected_category", sa.String(length=100), nullable=True),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_review_items_fingerprint"), "review_items", ["fingerprint"], unique=False
    )
    op.create_index(op.f("ix_review_items_status"), "review_items", ["status"], unique=False)
    op.create_index(op.f("ix_review_items_trace_id"), "review_items", ["trace_id"], unique=False)

    op.create_table(
        "rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("merchant_pattern", sa.String(length=255), nullable=False),
        sa.Column(
            "match_type",
            sa.Enum(
                "EXACT",
                "CONTAINS",
                "REGEX",
                name="rulematchtype",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("budget", sa.String(length=100), nullable=True),
        sa.Column(
            "origin",
            sa.Enum(
                "MANUAL",
                "CORRECTION",
                "SEED",
                name="ruleorigin",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("merchant_pattern", "match_type", name="uq_rule_pattern"),
    )
    op.create_index(op.f("ix_rules_merchant_pattern"), "rules", ["merchant_pattern"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_rules_merchant_pattern"), table_name="rules")
    op.drop_table("rules")
    op.drop_index(op.f("ix_review_items_trace_id"), table_name="review_items")
    op.drop_index(op.f("ix_review_items_status"), table_name="review_items")
    op.drop_index(op.f("ix_review_items_fingerprint"), table_name="review_items")
    op.drop_table("review_items")
    op.drop_index(op.f("ix_ingested_transactions_trace_id"), table_name="ingested_transactions")
    op.drop_index(op.f("ix_ingested_transactions_fingerprint"), table_name="ingested_transactions")
    op.drop_table("ingested_transactions")
    op.drop_index(op.f("ix_audit_logs_trace_id"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_event"), table_name="audit_logs")
    op.drop_table("audit_logs")
