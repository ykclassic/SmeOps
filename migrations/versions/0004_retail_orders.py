"""Create retail catalogue and order lifecycle.

Revision ID: 0004_retail_orders
Revises: 0003_business_operations
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_retail_orders"
down_revision: str | None = "0003_business_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("price_minor", sa.BigInteger(), nullable=False),
        sa.Column("stock_quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "price_minor >= 0",
            name="ck_products_product_price_nonnegative",
        ),
        sa.CheckConstraint(
            "stock_quantity >= 0",
            name="ck_products_product_stock_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_products_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_products"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_products_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "sku", name="uq_products_tenant_sku"),
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("total_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="pending_approval",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "total_minor >= 0",
            name="ck_orders_order_total_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('pending_approval', 'approved', 'cancelled', 'fulfilled')",
            name="ck_orders_order_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_orders_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            name="fk_orders_tenant_customer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_orders_tenant_creator",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_orders_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_orders_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_orders_tenant_created",
        "orders",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "order_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("product_name", sa.String(length=160), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_minor", sa.BigInteger(), nullable=False),
        sa.Column("line_total_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "quantity > 0",
            name="ck_order_items_order_item_quantity_positive",
        ),
        sa.CheckConstraint(
            "unit_price_minor >= 0",
            name="ck_order_items_order_item_price_nonnegative",
        ),
        sa.CheckConstraint(
            "line_total_minor >= 0",
            name="ck_order_items_order_item_total_nonnegative",
        ),
        sa.CheckConstraint(
            "line_total_minor = unit_price_minor * quantity",
            name="ck_order_items_order_item_line_total_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_order_items_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "order_id"],
            ["orders.tenant_id", "orders.id"],
            name="fk_order_items_tenant_order",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["products.tenant_id", "products.id"],
            name="fk_order_items_tenant_product",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_items"),
        sa.UniqueConstraint(
            "tenant_id",
            "order_id",
            "product_id",
            name="uq_order_items_order_product",
        ),
    )
    op.create_index(
        "ix_order_items_tenant_order",
        "order_items",
        ["tenant_id", "order_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_order_items_tenant_order", table_name="order_items")
    op.drop_table("order_items")
    op.drop_index("ix_orders_tenant_created", table_name="orders")
    op.drop_table("orders")
    op.drop_table("products")
