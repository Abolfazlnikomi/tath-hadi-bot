from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, inspect
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
    __table_args__ = (UniqueConstraint("consultation_id", "booked_by"),)
    slot_id: Mapped[int] = mapped_column(ForeignKey("consultation_slots.id"), primary_key=True)
    consultation_id: Mapped[int] = mapped_column(ForeignKey("consultations.id"))
    booked_by: Mapped[int] = mapped_column(BigInteger)
    student_names: Mapped[str] = mapped_column(Text)
    booked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConsultationFamilyClaim(Base):
    __tablename__ = "consultation_family_claims"
    consultation_id: Mapped[int] = mapped_column(ForeignKey("consultations.id"), primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("consultation_bookings.slot_id"))


def migrate_consultation_bookings(connection):
    inspector = inspect(connection)
    if not any(column["name"] == "guardian_id" for column in inspector.get_columns("consultation_bookings")):
        return
    if connection.dialect.name == "sqlite":
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")
        connection.exec_driver_sql("ALTER TABLE consultation_family_claims RENAME TO consultation_family_claims_legacy")
        connection.exec_driver_sql("ALTER TABLE consultation_bookings RENAME TO consultation_bookings_legacy")
        ConsultationBooking.__table__.create(connection)
        ConsultationFamilyClaim.__table__.create(connection)
        connection.exec_driver_sql("""
            INSERT INTO consultation_bookings (slot_id, consultation_id, booked_by, student_names, booked_at)
            SELECT slot_id, consultation_id, guardian_id, student_names, booked_at FROM consultation_bookings_legacy
        """)
        connection.exec_driver_sql("""
            INSERT INTO consultation_family_claims (consultation_id, student_id, slot_id)
            SELECT consultation_id, student_id, slot_id FROM consultation_family_claims_legacy
        """)
        connection.exec_driver_sql("DROP TABLE consultation_family_claims_legacy")
        connection.exec_driver_sql("DROP TABLE consultation_bookings_legacy")
    else:
        for constraint in inspector.get_foreign_keys("consultation_bookings"):
            if constraint["constrained_columns"] == ["guardian_id"]:
                name = connection.dialect.identifier_preparer.quote_identifier(constraint["name"])
                connection.exec_driver_sql(f"ALTER TABLE consultation_bookings DROP CONSTRAINT {name}")
        connection.exec_driver_sql("ALTER TABLE consultation_bookings RENAME COLUMN guardian_id TO booked_by")
