from datetime import time
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator


class ServiceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    duration_minutes: int = Field(ge=5, le=480)
    price_minor: int = Field(ge=0, le=1_000_000_000_000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("Service name must contain at least two non-space characters")
        return normalized


class ServiceResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    duration_minutes: int
    price_minor: int
    currency: str
    is_active: bool


class BusinessHoursDay(BaseModel):
    weekday: int = Field(ge=0, le=6, description="Monday=0 through Sunday=6")
    is_closed: bool = False
    opens_at: time | None = None
    closes_at: time | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "BusinessHoursDay":
        if self.is_closed:
            if self.opens_at is not None or self.closes_at is not None:
                raise ValueError("Closed days must not specify opening or closing times")
        elif (
            self.opens_at is None
            or self.closes_at is None
            or self.opens_at >= self.closes_at
        ):
            raise ValueError("Open days require a valid opening and closing time")
        return self


class BusinessHoursReplace(BaseModel):
    days: list[BusinessHoursDay] = Field(min_length=7, max_length=7)

    @model_validator(mode="after")
    def require_all_weekdays_once(self) -> "BusinessHoursReplace":
        weekdays = [day.weekday for day in self.days]
        if set(weekdays) != set(range(7)) or len(set(weekdays)) != 7:
            raise ValueError("Provide exactly one entry for each weekday, Monday through Sunday")
        return self


class BusinessHoursResponse(BaseModel):
    weekday: int
    is_closed: bool
    opens_at: time | None
    closes_at: time | None


class AppointmentCreate(BaseModel):
    service_id: UUID
    customer_name: str = Field(min_length=2, max_length=160)
    customer_phone: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    starts_at: AwareDatetime

    @field_validator("customer_name")
    @classmethod
    def normalize_customer_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("Customer name must contain at least two non-space characters")
        return normalized


class AppointmentResponse(BaseModel):
    id: UUID
    service_id: UUID
    customer_id: UUID
    customer_name: str
    customer_phone: str
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    status: str
