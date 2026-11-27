from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, utcnow


class Consultation(Base):
    __tablename__ = "consultations"
    id: Mapped[int] = mapped_column(primary_key=True)
    classroom_id: Mapped[int] = mapped_column(ForeignKey("classrooms.id"), index=True)
    title: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(1000))


class ConsultationSlot(Base):
    __tablename__ = "consultation_slots"
    __table_args__ = (UniqueConstraint("consultation_id", "starts_at"), CheckConstraint("ends_at > starts_at"))
    id: Mapped[int] = mapped_column(primary_key=True)
    consultation_id: Mapped[int] = mapped_column(ForeignKey("consultations.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConsultationBooking(Base):
    __tablename__ = "consultation_bookings"
    __table_args__ = (UniqueConstraint("consultation_id", "guardian_id"),)
    slot_id: Mapped[int] = mapped_column(ForeignKey("consultation_slots.id"), primary_key=True)
    consultation_id: Mapped[int] = mapped_column(ForeignKey("consultations.id"))
    guardian_id: Mapped[int] = mapped_column(ForeignKey("guardian_accounts.user_id"))
    student_names: Mapped[str] = mapped_column(Text)
    booked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConsultationFamilyClaim(Base):
    __tablename__ = "consultation_family_claims"
    consultation_id: Mapped[int] = mapped_column(ForeignKey("consultations.id"), primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("consultation_bookings.slot_id"))
