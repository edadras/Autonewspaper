"""SQLAlchemy declarative base and shared column helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp (SQLite stores naive values otherwise)."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for every ORM entity."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def to_dict(self, exclude: set[str] | None = None) -> dict[str, Any]:
        """Return a plain dictionary of column values."""
        exclude = exclude or set()
        out: dict[str, Any] = {}
        for column in self.__table__.columns:  # type: ignore[attr-defined]
            if column.name in exclude:
                continue
            value = getattr(self, column.name)
            out[column.name] = value.isoformat() if isinstance(value, datetime) else value
        return out


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` columns."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
