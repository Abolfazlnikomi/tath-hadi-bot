import re

from sqlalchemy import select

from .education_models import GuardianAccount, GuardianLink, Mentor, MentorClass, FamilyMessage, Homework, HomeworkSubmission
from .features import enabled
from .models import Classroom, Enrollment, Student
from .preferences import is_admin


MENTOR_ADMIN = "👥 مدیریت مربیان"
MENTOR_PANEL = "🎓 پنل آموزشی"
HOMEWORK_BUTTON = "📚 تکالیف من"
PERMISSIONS = {
    "roster": (1, "فهرست دانش‌آموزان"),
    "reports": (2, "گزارش جلسه"),
    "attendance": (4, "حضور و غیاب و پیگیری"),
    "homework": (8, "تکلیف و ارزیابی"),
    "family": (16, "ارتباط با خانواده"),
    "coins": (32, "ثبت سکه‌های هادی"),
}


def identifiers(args, *limits):
    if len(args) != len(limits) or any(not re.fullmatch(r"[0-9]{1,19}", arg) for arg in args):
        return None
    ids = tuple(map(int, args))
    return ids if all(value <= limit for value, limit in zip(ids, limits)) else None


ID_MAX = 2**31 - 1
USER_MAX = 2**63 - 1


def mentor_active(session, user_id):
    mentor = session.get(Mentor, user_id)
    return mentor is not None and mentor.active


def class_access(session, owner_id, user_id, classroom_id, feature=None):
    if session.get(Classroom, classroom_id) is None:
        return False
    if is_admin(session, user_id, owner_id):
        return True
    if not mentor_active(session, user_id):
        return False
    assignment = session.get(MentorClass, (user_id, classroom_id))
    return assignment is not None and (feature is None or bool(assignment.permissions & PERMISSIONS[feature][0]))


def class_students(classroom_id):
    return select(Student).join(Enrollment).where(
        Enrollment.classroom_id == classroom_id, Student.active.is_(True),
    )


def active_member(session, classroom_id, student_id):
    return session.scalar(class_students(classroom_id).where(Student.id == student_id))


def bound_student(session, user_id, chat_id):
    return session.scalar(select(Student).where(
        Student.bale_user_id == user_id, Student.chat_id == chat_id, Student.active.is_(True),
    ))


def family_recipients(session, classroom_id, student_id=None, guardian_id=None):
    recipients = select(GuardianAccount.user_id, GuardianAccount.chat_id).join(GuardianLink).join(
        Student, Student.id == GuardianLink.student_id,
    ).join(Enrollment, Enrollment.student_id == Student.id).where(
        Enrollment.classroom_id == classroom_id, Student.active.is_(True), GuardianAccount.chat_id.is_not(None),
    )
    if student_id is not None:
        recipients = recipients.where(Student.id == student_id)
    if guardian_id is not None:
        recipients = recipients.where(GuardianAccount.user_id == guardian_id)
    return session.execute(recipients.distinct()).all()


def valid_education_delivery(session, message, owner_id):
    parts = message.dedupe_key.split(":")
    chat_id = message.payload.get("chat_id")
    if parts[0] == "edview":
        _, actor_id, classroom_id, feature, student_id, _ = parts
        return (feature != "attendance" or enabled(session, "attendance")) and class_access(
            session, owner_id, int(actor_id), int(classroom_id), feature or None,
        ) and (
            student_id == "0" or active_member(session, int(classroom_id), int(student_id)) is not None
        )
    if parts[0] == "edfamily":
        _, message_id, guardian_id = parts
        sent = session.get(FamilyMessage, int(message_id))
        if sent is None or not class_access(session, owner_id, sent.actor_id, sent.classroom_id, "family"):
            return False
        return (int(guardian_id), chat_id) in family_recipients(
            session, sent.classroom_id, sent.student_id, int(guardian_id),
        )
    _, homework_id, student_id, _ = parts
    homework = session.get(Homework, int(homework_id))
    if homework is None:
        return False
    student = active_member(session, homework.classroom_id, int(student_id))
    return (student is not None and student.chat_id == chat_id and student.bale_user_id is not None
            and session.get(HomeworkSubmission, (homework.id, student.id)) is not None)
