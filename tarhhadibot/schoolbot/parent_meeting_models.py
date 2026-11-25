from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, utcnow


class MeetingSection(Base):
    __tablename__ = "parent_meeting_sections"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(80), unique=True)


class ParentMeeting(Base):
    __tablename__ = "parent_meetings"
    id: Mapped[int] = mapped_column(primary_key=True)
    section_id: Mapped[int] = mapped_column(ForeignKey("parent_meeting_sections.id"), index=True)
    topic: Mapped[str] = mapped_column(String(200))
    location: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    photo_id: Mapped[str | None] = mapped_column(String(512))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ParentMeetingClass(Base):
    __tablename__ = "parent_meeting_classes"
    meeting_id: Mapped[int] = mapped_column(ForeignKey("parent_meetings.id"), primary_key=True)
    classroom_id: Mapped[int] = mapped_column(ForeignKey("classrooms.id"), primary_key=True)


class ParentMeetingStage(Base):
    __tablename__ = "parent_meeting_stages"
    meeting_id: Mapped[int] = mapped_column(ForeignKey("parent_meetings.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    guardian_id: Mapped[int] = mapped_column(ForeignKey("guardian_accounts.user_id"), primary_key=True)
    stage: Mapped[str] = mapped_column(String(10), primary_key=True)
    response: Mapped[str | None] = mapped_column(String(3))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
