import hashlib
import json
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import CurrentUser, DbSession, require_roles
from app.models import AuditEvent, Customer, Order, OrderItem, Product, Tenant, User
from app.schemas.retail import (
    OrderCreate,
    OrderItemResponse,
    OrderResponse,
    ProductCreate,
    ProductResponse,
)

router = APIRouter(tags=["retail operations"])
ManagerOrOwner = Annotated[User, Depends(require_roles("owner", "manager"))]


async def _lock_tenant(session: DbSession, tenant_id: UUID) -> Tenant:
    result = await session.execute(
        select(Tenant).where(Tenant.id == tenant_id, Tenant.status == "active").with_for_update()
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=401, detail="Business account is unavailable")
    return tenant


def _product_response(product: Product) -> ProductResponse:
    return ProductResponse(
        id=product.id,
        sku=product.sku,
        name=product.name,
        description=product.description,
        price_minor=product.price_minor,
        stock_quantity=product.stock_quantity,
        is_active=product.is_active,
    )


def _fingerprint(payload: OrderCreate) -> str:
    canonical = {
        "customer_name": payload.customer_name,
        "customer_phone": payload.customer_phone,
        "items": [
            {"product_id": str(item.product_id), "quantity": item.quantity}
            for item in sorted(payload.items, key=lambda item: str(item.product_id))
        ],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _order_response(
    order: Order,
    customer: Customer,
    items: list[OrderItem],
) -> OrderResponse:
    return OrderResponse(
        id=order.id,
        customer_id=customer.id,
        customer_name=customer.name,
        customer_phone=customer.phone_e164,
        items=[
            OrderItemResponse(
                product_id=item.product_id,
                product_name=item.product_name,
                quantity=item.quantity,
                unit_price_minor=item.unit_price_minor,
                line_total_minor=item.line_total_minor,
            )
            for item in items
        ],
        total_minor=order.total_minor,
        status=order.status,
        created_at=order.created_at.isoformat(),
    )


async def _load_order(
    session: DbSession,
    tenant_id: UUID,
    order_id: UUID,
    *,
    lock: bool = False,
) -> tuple[Order, Customer, list[OrderItem]]:
    statement = select(Order).where(Order.id == order_id, Order.tenant_id == tenant_id)
    if lock:
        statement = statement.with_for_update()
    order_result = await session.execute(statement)
    order = order_result.scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    customer_result = await session.execute(
        select(Customer).where(
            Customer.id == order.customer_id,
            Customer.tenant_id == tenant_id,
        )
    )
    customer = customer_result.scalar_one()
    items_result = await session.execute(
        select(OrderItem)
        .where(OrderItem.order_id == order.id, OrderItem.tenant_id == tenant_id)
        .order_by(OrderItem.product_name)
    )
    return order, customer, list(items_result.scalars().all())


@router.post("/products", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductCreate,
    session: DbSession,
    user: ManagerOrOwner,
) -> ProductResponse:
    await _lock_tenant(session, user.tenant_id)
    product = Product(
        id=uuid4(),
        tenant_id=user.tenant_id,
        sku=payload.sku,
        name=payload.name,
        description=payload.description,
        price_minor=payload.price_minor,
        stock_quantity=payload.stock_quantity,
        is_active=True,
    )
    session.add(product)
    session.add(
        AuditEvent(
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            event_type="product.created",
            subject_type="product",
            subject_id=product.id,
            payload={
                "sku": product.sku,
                "price_minor": product.price_minor,
                "stock_quantity": product.stock_quantity,
            },
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Product SKU already exists") from exc
    return _product_response(product)


@router.get("/products", response_model=list[ProductResponse])
async def list_products(
    session: DbSession,
    user: CurrentUser,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ProductResponse]:
    result = await session.execute(
        select(Product)
        .where(Product.tenant_id == user.tenant_id, Product.is_active.is_(True))
        .order_by(Product.name)
        .limit(limit)
        .offset(offset)
    )
    return [_product_response(product) for product in result.scalars().all()]


@router.post("/orders", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def create_order(payload: OrderCreate, session: DbSession, user: CurrentUser) -> OrderResponse:
    tenant = await _lock_tenant(session, user.tenant_id)
    fingerprint = _fingerprint(payload)

    existing_result = await session.execute(
        select(Order).where(
            Order.tenant_id == tenant.id,
            Order.idempotency_key == payload.idempotency_key,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was already used for a different order",
            )
        order, customer, items = await _load_order(session, tenant.id, existing.id)
        return _order_response(order, customer, items)

    product_ids = [item.product_id for item in payload.items]
    products_result = await session.execute(
        select(Product)
        .where(
            Product.tenant_id == tenant.id,
            Product.id.in_(product_ids),
            Product.is_active.is_(True),
        )
        .order_by(Product.id)
        .with_for_update()
    )
    products = {product.id: product for product in products_result.scalars().all()}
    if len(products) != len(product_ids):
        raise HTTPException(status_code=404, detail="One or more products were not found")

    customer_result = await session.execute(
        select(Customer).where(
            Customer.tenant_id == tenant.id,
            Customer.phone_e164 == payload.customer_phone,
        )
    )
    customer = customer_result.scalar_one_or_none()
    if customer is None:
        customer = Customer(
            id=uuid4(),
            tenant_id=tenant.id,
            name=payload.customer_name,
            phone_e164=payload.customer_phone,
        )
        session.add(customer)
        await session.flush()

    order_id = uuid4()
    order_items: list[OrderItem] = []
    total_minor = 0
    for requested_item in payload.items:
        product = products[requested_item.product_id]
        line_total = product.price_minor * requested_item.quantity
        total_minor += line_total
        order_items.append(
            OrderItem(
                id=uuid4(),
                tenant_id=tenant.id,
                order_id=order_id,
                product_id=product.id,
                product_name=product.name,
                quantity=requested_item.quantity,
                unit_price_minor=product.price_minor,
                line_total_minor=line_total,
            )
        )

    order = Order(
        id=order_id,
        tenant_id=tenant.id,
        customer_id=customer.id,
        created_by_user_id=user.id,
        idempotency_key=payload.idempotency_key,
        request_fingerprint=fingerprint,
        total_minor=total_minor,
        status="pending_approval",
    )
    session.add(order)
    session.add_all(order_items)
    session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="order.created",
            subject_type="order",
            subject_id=order.id,
            payload={"total_minor": total_minor, "item_count": len(order_items)},
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Order could not be created") from exc

    return _order_response(order, customer, order_items)


@router.get("/orders", response_model=list[OrderResponse])
async def list_orders(
    session: DbSession,
    user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[OrderResponse]:
    order_result = await session.execute(
        select(Order)
        .where(Order.tenant_id == user.tenant_id)
        .order_by(Order.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    orders = list(order_result.scalars().all())
    if not orders:
        return []

    order_ids = [order.id for order in orders]
    customer_ids = [order.customer_id for order in orders]
    customers_result = await session.execute(
        select(Customer).where(
            Customer.tenant_id == user.tenant_id,
            Customer.id.in_(customer_ids),
        )
    )
    customers = {customer.id: customer for customer in customers_result.scalars().all()}
    items_result = await session.execute(
        select(OrderItem)
        .where(
            OrderItem.tenant_id == user.tenant_id,
            OrderItem.order_id.in_(order_ids),
        )
        .order_by(OrderItem.product_name)
    )
    items_by_order: dict[UUID, list[OrderItem]] = {}
    for item in items_result.scalars().all():
        items_by_order.setdefault(item.order_id, []).append(item)

    return [
        _order_response(order, customers[order.customer_id], items_by_order.get(order.id, []))
        for order in orders
    ]


@router.post("/orders/{order_id}/approve", response_model=OrderResponse)
async def approve_order(
    order_id: UUID,
    session: DbSession,
    user: ManagerOrOwner,
) -> OrderResponse:
    tenant = await _lock_tenant(session, user.tenant_id)
    order, customer, items = await _load_order(session, tenant.id, order_id, lock=True)
    if order.status != "pending_approval":
        raise HTTPException(status_code=409, detail="Order is not awaiting approval")

    product_ids = sorted({item.product_id for item in items}, key=str)
    products_result = await session.execute(
        select(Product)
        .where(Product.tenant_id == tenant.id, Product.id.in_(product_ids))
        .order_by(Product.id)
        .with_for_update()
    )
    products = {product.id: product for product in products_result.scalars().all()}

    for item in items:
        product = products.get(item.product_id)
        if product is None or product.stock_quantity < item.quantity:
            raise HTTPException(
                status_code=409,
                detail=f"Insufficient inventory for {item.product_name}",
            )

    for item in items:
        products[item.product_id].stock_quantity -= item.quantity

    order.status = "approved"
    session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="order.approved",
            subject_type="order",
            subject_id=order.id,
            payload={"total_minor": order.total_minor},
        )
    )
    await session.commit()
    return _order_response(order, customer, items)


@router.post("/orders/{order_id}/cancel", response_model=OrderResponse)
async def cancel_order(
    order_id: UUID,
    session: DbSession,
    user: ManagerOrOwner,
) -> OrderResponse:
    tenant = await _lock_tenant(session, user.tenant_id)
    order, customer, items = await _load_order(session, tenant.id, order_id, lock=True)
    if order.status != "pending_approval":
        raise HTTPException(
            status_code=409,
            detail="Only orders awaiting approval can be cancelled",
        )

    order.status = "cancelled"
    session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="order.cancelled",
            subject_type="order",
            subject_id=order.id,
            payload={},
        )
    )
    await session.commit()
    return _order_response(order, customer, items)


@router.post("/orders/{order_id}/fulfill", response_model=OrderResponse)
async def fulfill_order(
    order_id: UUID,
    session: DbSession,
    user: ManagerOrOwner,
) -> OrderResponse:
    tenant = await _lock_tenant(session, user.tenant_id)
    order, customer, items = await _load_order(session, tenant.id, order_id, lock=True)
    if order.status != "approved":
        raise HTTPException(status_code=409, detail="Only approved orders can be fulfilled")

    order.status = "fulfilled"
    session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="order.fulfilled",
            subject_type="order",
            subject_id=order.id,
            payload={},
        )
    )
    await session.commit()
    return _order_response(order, customer, items)
