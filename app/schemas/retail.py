from uuid import UUID

from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
)


class ProductCreate(BaseModel):
    sku: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
    name: str = Field(min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    price_minor: int = Field(ge=0, le=1_000_000_000_000)
    stock_quantity: int = Field(ge=0, le=1_000_000_000)

    @field_validator("sku")
    @classmethod
    def normalize_sku(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("Product name must contain at least two non-space characters")
        return normalized


class ProductResponse(BaseModel):
    id: UUID
    sku: str
    name: str
    description: str | None
    price_minor: int
    stock_quantity: int
    currency: str = "NGN"
    is_active: bool


class OrderItemCreate(BaseModel):
    product_id: UUID
    quantity: int = Field(ge=1, le=1000)


class OrderCreate(BaseModel):
    idempotency_key: UUID
    customer_name: str = Field(min_length=2, max_length=160)
    customer_phone: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    items: list[OrderItemCreate] = Field(min_length=1, max_length=50)

    @field_validator("customer_name")
    @classmethod
    def normalize_customer_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("Customer name must contain at least two non-space characters")
        return normalized

    @model_validator(mode="after")
    def reject_duplicate_products(self) -> "OrderCreate":
        product_ids = [item.product_id for item in self.items]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("Each product may appear only once in an order")
        return self


class OrderItemResponse(BaseModel):
    product_id: UUID
    product_name: str
    quantity: int
    unit_price_minor: int
    line_total_minor: int


class OrderResponse(BaseModel):
    id: UUID
    customer_id: UUID
    customer_name: str
    customer_phone: str
    items: list[OrderItemResponse]
    total_minor: int
    currency: str = "NGN"
    status: str
    created_at: str
