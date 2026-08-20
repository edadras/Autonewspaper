"""Re-export of the declarative base.

The base class lives in :mod:`app.models.base` so that the ORM entities do not
have to import the ``app.database`` package (which itself depends on the
entities). This module keeps ``app.database.base`` working as an import path.
"""

from app.models.base import Base, TimestampMixin, utcnow

__all__ = ["Base", "TimestampMixin", "utcnow"]
