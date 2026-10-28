from datetime import date

import jdatetime
from sqlalchemy import select

from .models import Attendance, Classroom, Discount, Enrollment, Outbox, Student, utcnow


def members(session, classroom_id):
    return list(session.scalars(
        select(Student).join(Enrollment).where(
            Enrollment.classroom_id == classroom_id, Student.active.is_(True),
        ).order_by(Student.full_name, Student.id)
    ))


def validate_import(session, pending):
    if session.get(Classroom, pending["classroom_id"]) is None:
        raise ValueError("کلاس انتخاب‌شده وجود ندارد.")
    if pending["kind"] == "students":
        return
    roster = {student.national_id: student for student in members(session, pending["classroom_id"])}
    for index, row in enumerate(pending["rows"], 2):
        student = roster.get(row["national_id"])
        if student is None:
            raise ValueError(f"ردیف {index}: دانش‌آموز عضو فعال این کلاس نیست.")
        if row["name"] != student.full_name:
            raise ValueError(f"ردیف {index}: نام با کد ملی در سامانه مطابقت ندارد.")


def apply_import(session, pending):
    validate_import(session, pending)
    classroom_id = pending["classroom_id"]
    count = 0
    for row in pending["rows"]:
        student = session.scalar(select(Student).where(Student.national_id == row["national_id"]))
        if pending["kind"] == "students":
            if student is None:
                student = Student(national_id=row["national_id"], full_name=row["name"], phone=row["phone"])
                session.add(student)
                session.flush()
            else:
                student.full_name = row["name"]
                student.phone = row["phone"]
                student.active = True
            if session.get(Enrollment, (classroom_id, student.id)) is None:
                session.add(Enrollment(classroom_id=classroom_id, student_id=student.id))
        elif pending["kind"] == "attendance":
            session_date = date.fromisoformat(pending["session_date"])
            attendance = session.scalar(select(Attendance).where(
                Attendance.classroom_id == classroom_id,
                Attendance.student_id == student.id,
                Attendance.session_date == session_date,
            ))
            if attendance and attendance.status == row["status"]:
                continue
            jalali = jdatetime.date.fromgregorian(date=session_date).strftime("%Y/%m/%d")
            if attendance is None:
                attendance = Attendance(
                    classroom_id=classroom_id, student_id=student.id,
                    session_date=session_date, jalali_date=jalali, status=row["status"],
                )
                session.add(attendance)
            else:
                attendance.status = row["status"]
                attendance.updated_at = utcnow()
            if student.chat_id is not None:
                classroom = session.get(Classroom, classroom_id)
                session.add(Outbox(method="sendMessage", payload={
                    "chat_id": student.chat_id,
                    "text": f"دانش‌آموز گرامی، وضعیت شما برای تاریخ {jalali} ثبت شد: {row['status']}\nکلاس: {classroom.title}",
                }))
        else:
            discount = session.scalar(select(Discount).where(
                Discount.classroom_id == classroom_id, Discount.student_id == student.id,
                Discount.code == row["code"],
            ))
            if discount is None:
                session.add(Discount(classroom_id=classroom_id, student_id=student.id,
                                     code=row["code"], description=row["description"]))
            else:
                discount.description = row["description"]
        count += 1
    return count
