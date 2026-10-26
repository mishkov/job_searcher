import asyncio

from sqlalchemy import BigInteger, Engine, JSON, Text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from src.storage.job_post_repository import JobPostRepository, SavedJobPost
from src.storage.mysql.base import Base


class JobPostModel(Base):
    __tablename__ = 'saved_job_posts'
    __table_args__ = {'mysql_charset': 'utf8mb4'}

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    entities: Mapped[list[dict]] = mapped_column(JSON)
    reply_markup: Mapped[dict] = mapped_column(JSON)

    def snapshot(self) -> SavedJobPost:
        return SavedJobPost(self.chat_id, self.message_id, self.title, self.text,
                            self.entities, self.reply_markup)


class MysqlJobPostRepository(JobPostRepository):
    def __init__(self, engine: Engine):
        self.engine = engine

    async def get(self, chat_id: int, message_id: int) -> SavedJobPost | None:
        return await asyncio.to_thread(self._get, chat_id, message_id)

    def _get(self, chat_id: int, message_id: int) -> SavedJobPost | None:
        with Session(self.engine) as session:
            row = session.get(JobPostModel, (chat_id, message_id))
            return row.snapshot() if row else None

    async def save(self, post: SavedJobPost) -> SavedJobPost:
        return await asyncio.to_thread(self._save, post)

    def _save(self, post: SavedJobPost) -> SavedJobPost:
        with Session(self.engine) as session:
            key = (post.chat_id, post.message_id)
            row = session.get(JobPostModel, key)
            if row is None:
                row = JobPostModel(**vars(post))
                session.add(row)
                try:
                    session.commit()
                except IntegrityError:
                    # Another callback may have saved this message first.
                    session.rollback()
                    row = session.get(JobPostModel, key)
                    if row is None:
                        raise
            return row.snapshot()
