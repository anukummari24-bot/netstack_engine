from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WebhookEvent(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_delivery_schedule", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[object] = mapped_column(JSON, nullable=False)
    webhook_url: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=utc_now
    )
    attempts: Mapped[list["DeliveryAttempt"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="DeliveryAttempt.id",
    )


class DeliveryAttempt(Base):
    __tablename__ = "attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    event: Mapped[WebhookEvent] = relationship(back_populates="attempts")
