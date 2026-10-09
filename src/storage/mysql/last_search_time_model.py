from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from src.storage.mysql.base import Base

class LastSearchTimeModel(Base):
    __tablename__ = "last_search_time"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_searched_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )