import asyncio
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Engine, Integer, String, inspect, text
from sqlalchemy.orm import Mapped, Session, mapped_column

from src.monitoring import MonitoringState, SleepPeriod
from src.storage.mysql.base import Base


class MonitoringModel(Base):
    __tablename__ = 'monitoring_settings'

    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    sleep_start: Mapped[int | None] = mapped_column(Integer)
    sleep_end: Mapped[int | None] = mapped_column(Integer)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_result: Mapped[str] = mapped_column(String(500), nullable=False)


class MysqlMonitoringStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    async def load(self) -> MonitoringState:
        return await asyncio.to_thread(self._load)

    def _load(self) -> MonitoringState:
        # create_all leaves existing tables intact; upgrade the earlier singleton schema.
        columns = inspect(self.engine).get_columns(MonitoringModel.__tablename__)
        if not any(column['name'] == 'chat_id' for column in columns):
            with self.engine.begin() as connection:
                connection.execute(text('ALTER TABLE monitoring_settings ADD COLUMN chat_id BIGINT NULL'))
        with Session(self.engine) as session:
            row = session.get(MonitoringModel, 1)
            if row is None:
                return MonitoringState()
            def utc(value):
                return value.replace(tzinfo=timezone.utc) if value else None
            return MonitoringState(
                enabled=row.enabled, chat_id=row.chat_id,
                sleep=SleepPeriod(row.sleep_start, row.sleep_end) if row.sleep_start is not None else None,
                next_check_at=utc(row.next_check_at), last_started_at=utc(row.last_started_at),
                last_completed_at=utc(row.last_completed_at), last_result=row.last_result,
            )

    async def save(self, state: MonitoringState) -> None:
        await asyncio.to_thread(self._save, state)

    def _save(self, state: MonitoringState) -> None:
        def naive_utc(value):
            return value.astimezone(timezone.utc).replace(tzinfo=None) if value else None
        with Session(self.engine) as session:
            session.merge(MonitoringModel(
                id=1, enabled=state.enabled, chat_id=state.chat_id,
                sleep_start=state.sleep.start if state.sleep else None,
                sleep_end=state.sleep.end if state.sleep else None,
                next_check_at=naive_utc(state.next_check_at),
                last_started_at=naive_utc(state.last_started_at),
                last_completed_at=naive_utc(state.last_completed_at), last_result=state.last_result,
            ))
            session.commit()
