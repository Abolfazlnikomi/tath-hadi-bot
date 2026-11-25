from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, utcnow


class Mentor(Base):
    __tablename__ = "mentors"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class MentorClass(Base):
    __tablename__ = "mentor_classes"
    mentor_id: Mapped[int] = mapped_column(ForeignKey("mentors.user_id"), primary_key=True)
    classroom_id: Mapped[int] = mapped_column(ForeignKey("classrooms.id"), primary_key=True)
    permissions: Mapped[int] = mapped_column(Integer, default=0)


class MentorRegistration(Base):
    __tablename__ = "mentor_registrations"
    national_id: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(20))
    activation_hash: Mapped[str | None] = mapped_column(String(64))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("mentors.user_id"), unique=True)


class SessionReport(Base):
    __tablename__ = "education_session_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    classroom_id: Mapped[int] = mapped_column(ForeignKey("classrooms.id"), index=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    session_date: Mapped[date] = mapped_column(Date)
    topic: Mapped[str] = mapped_column(String(200))
    presence: Mapped[str] = mapped_column(String(300))
    activity: Mapped[str] = mapped_column(String(600))
    next_action: Mapped[str] = mapped_column(String(600))


class AbsenceFollowup(Base):
    __tablename__ = "education_absence_followups"
    attendance_id: Mapped[int] = mapped_column(ForeignKey("attendance.id"), primary_key=True)
    reason: Mapped[str] = mapped_column(String(600))
    actor_id: Mapped[int] = mapped_column(BigInteger)
    referred: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)


class Homework(Base):
    __tablename__ = "education_homework"
    id: Mapped[int] = mapped_column(primary_key=True)
    classroom_id: Mapped[int] = mapped_column(ForeignKey("classrooms.id"), index=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(200))
    instructions: Mapped[str] = mapped_column(String(1500))
    due_date: Mapped[date] = mapped_column(Date)


class HomeworkSubmission(Base):
    __tablename__ = "education_homework_submissions"
    homework_id: Mapped[int] = mapped_column(ForeignKey("education_homework.id"), primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), primary_key=True)
    body: Mapped[str] = mapped_column(String(2200))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    feedback: Mapped[str | None] = mapped_column(String(1200))
    reviewer_id: Mapped[int | None] = mapped_column(BigInteger)


class GuardianAccount(Base):
    __tablename__ = "guardian_accounts"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)


class GuardianLink(Base):
    __tablename__ = "guardian_links"
    __table_args__ = (Index("ix_guardian_links_account", "guardian_id"),)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), primary_key=True)
    guardian_id: Mapped[int] = mapped_column(ForeignKey("guardian_accounts.user_id"), primary_key=True)


class FamilyMessage(Base):
    __tablename__ = "education_family_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    classroom_id: Mapped[int] = mapped_column(ForeignKey("classrooms.id"), index=True)
    student_id: Mapped[int | None] = mapped_column(ForeignKey("students.id"))
    actor_id: Mapped[int] = mapped_column(BigInteger)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
