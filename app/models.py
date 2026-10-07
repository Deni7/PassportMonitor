from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain import utcnow


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    username: Mapped[str | None] = mapped_column(String)
    full_name: Mapped[str | None] = mapped_column(String)
    language_code: Mapped[str | None] = mapped_column(String)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class ProviderRecord(Base):
    __tablename__ = "providers"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class LocationRecord(Base):
    __tablename__ = "locations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"))
    data: Mapped[dict] = mapped_column(JSON)


class DepartmentRecord(Base):
    __tablename__ = "departments"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"))
    data: Mapped[dict] = mapped_column(JSON)


class ServiceRecord(Base):
    __tablename__ = "services"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)


class ProviderService(Base):
    __tablename__ = "provider_services"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"))
    canonical_service_id: Mapped[str] = mapped_column(ForeignKey("services.id"))
    data: Mapped[dict] = mapped_column(JSON)


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider_mode: Mapped[str] = mapped_column(String)
    location_id: Mapped[str] = mapped_column(String)
    location_name: Mapped[str] = mapped_column(String)
    locations: Mapped[dict] = mapped_column(JSON)
    canonical_service_id: Mapped[str] = mapped_column(ForeignKey("services.id"))
    date_from: Mapped[date] = mapped_column(Date)
    date_to: Mapped[date] = mapped_column(Date)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PollJob(Base):
    __tablename__ = "poll_jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"))
    next_run: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, default="NEW")
    failures: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict] = mapped_column(JSON)


class SlotRecord(Base):
    __tablename__ = "slots"
    fingerprint: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("poll_jobs.id"), index=True)
    state: Mapped[str] = mapped_column(String)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    disappeared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSON)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("telegram_user_id", "fingerprint", "generation"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    fingerprint: Mapped[str] = mapped_column(ForeignKey("slots.fingerprint"))
    generation: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[str] = mapped_column(ForeignKey("poll_jobs.id"), index=True)
    status: Mapped[str] = mapped_column(String, default="PENDING", index=True)
    data: Mapped[dict] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderHealth(Base):
    __tablename__ = "provider_health"
    id: Mapped[str] = mapped_column(ForeignKey("providers.id"), primary_key=True)
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String)
    response_time: Mapped[float] = mapped_column(Float, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_available_slots: Mapped[int] = mapped_column(Integer, default=0)
    last_alert: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BotEvent(Base):
    __tablename__ = "bot_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, index=True)
    text: Mapped[str] = mapped_column(String, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)


class PollRun(Base):
    __tablename__ = "poll_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("poll_jobs.id"), index=True)
    provider_id: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="RUNNING", index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    slot_count: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict] = mapped_column(JSON)


class PollRunSubscription(Base):
    __tablename__ = "poll_run_subscriptions"
    run_id: Mapped[int] = mapped_column(ForeignKey("poll_runs.id"), primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)


class ProviderRequest(Base):
    __tablename__ = "provider_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("poll_runs.id"), index=True)
    operation: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="RUNNING", index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    data: Mapped[dict] = mapped_column(JSON)
