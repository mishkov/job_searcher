from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from src.storage.mysql.base import Base


class SourceSearchHistoryModel(Base):
    __tablename__ = 'source_search_history'

    source_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_successful_started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
