from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from src.storage.mysql.base import Base

class SeenJobModel(Base):
    __tablename__ = "seen_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_fingerprint: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )