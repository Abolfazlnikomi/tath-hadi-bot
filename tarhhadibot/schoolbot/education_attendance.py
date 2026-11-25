import secrets
from datetime import date

from sqlalchemy import select

from . import excel
from .education_access import ID_MAX, active_member, identifiers
from .education_models import AbsenceFollowup
from .models import Attendance, Enrollment, Student
from .preferences import is_admin
from .service import apply_import, members


ATTENDANCE_ROUTES = {
    "attendance": ("attendance", 2), "roll": ("attendance", 1),
    "absences": ("attendance", 2), "referrals": ("attendance", 2),
    "absenceview": ("attendance", 2), "followup": ("attendance", 2), "resolve": ("attendance", 2),
}
ROLL_SIZE = 8


def _attendance_version(session, data):
    rows = session.execute(select(Attendance.student_id, Attendance.status, Attendance.updated_at).where(
        Attendance.classroom_id == data["classroom_id"], Attendance.session_date == date.fromisoformat(data["session_date"]),
    ))
    return {str(student_id): [status, updated_at.isoformat()] for student_id, status, updated_at in rows
            if str(student_id) in data["statuses"]}


class EducationAttendanceMixin:
    def education_attendance_action(self, session, chat_id, user_id, state, admin, classroom, action, values):
        classroom_id = classroom.id
        back = f"ed:attendance:{classroom_id}:0"
        scope = (user_id, classroom_id, "attendance", 0)
        if action == "attendance":
            self.education_say(session, chat_id, "حضور و غیاب و پیگیری", [
                [("ثبت گروهی حضور امروز", f"ed:roll:{classroom_id}")],
                [("غیبت‌های ثبت‌شده", f"ed:absences:{classroom_id}:0")],
                [("ارجاع‌های باز", f"ed:referrals:{classroom_id}:0")],
                [("بازگشت", f"ed:class:{classroom_id}")],
            ])
            return
        if action == "roll":
            roster = members(session, classroom_id)
            if not roster:
                self.say(session, chat_id, "این کلاس دانش‌آموز فعال ندارد.")
                return
            today, _ = excel.today()
            self.set_state(state, mode="education_roll", operation="roll", classroom_id=classroom_id,
                           session_date=today.isoformat(), roster=sorted(student.id for student in roster),
                           statuses={}, nonce=secrets.token_hex(8))
            self.education_roll_page(session, chat_id, user_id, state, roster, 0)
            return
        if action in ("absences", "referrals"):
            query = select(Attendance).join(Student, Student.id == Attendance.student_id).where(
                Attendance.classroom_id == classroom_id, Attendance.status == "غایب",
            ).order_by(Attendance.session_date.desc(), Attendance.id.desc())
            if not admin:
                query = query.join(Enrollment, (Enrollment.student_id == Student.id) &
                                   (Enrollment.classroom_id == classroom_id)).where(Student.active.is_(True))
            if action == "referrals":
                query = query.join(AbsenceFollowup).where(
                    AbsenceFollowup.referred.is_(True), AbsenceFollowup.resolved.is_(False),
                )
            self.education_page(session, chat_id, query, values[1], f"ed:{action}:{classroom_id}",
                                "ارجاع‌های حل‌نشده" if action == "referrals" else "غیبت‌های کلاس",
                                lambda item: (f"{item.jalali_date} | {session.get(Student, item.student_id).full_name}",
                                              f"ed:absenceview:{classroom_id}:{item.id}"),
                                [("بازگشت", back)], scope)
            return
        attendance = session.get(Attendance, values[1])
        if attendance is None or attendance.classroom_id != classroom_id or attendance.status != "غایب" or (
                not admin and not active_member(session, classroom_id, attendance.student_id)):
            self.say(session, chat_id, "این غیبت در کلاس قابل دسترسی نیست.")
            return
        student = session.get(Student, attendance.student_id)
        record = session.get(AbsenceFollowup, attendance.id)
        source = f"ed:absenceview:{classroom_id}:{attendance.id}"
        if action == "followup":
            self.education_form(session, chat_id, state, "followup",
                                "علت غیبت را در خط اول و برای ارجاع در خط دوم «ارجاع» یا «بدون ارجاع» بنویسید.\n"
                                "اگر علت مشخص نیست، «نامشخص» وارد کنید. فقط اطلاعات لازم برای پیگیری آموزشی را بنویسید.",
                                classroom_id=classroom_id, attendance_id=attendance.id)
        elif action == "resolve":
            if not admin or record is None or not record.referred or record.resolved:
                self.say(session, chat_id, "بستن ارجاع فقط برای ادمین و ارجاع باز امکان‌پذیر است.")
                return
            self.education_preview(session, chat_id, state, "resolve", "این ارجاع به‌عنوان رسیدگی‌شده بسته شود؟",
                                   source, classroom_id=classroom_id, attendance_id=attendance.id)
        else:
            rows = [[("ثبت علت / ارجاع", f"ed:followup:{classroom_id}:{attendance.id}")]]
            if admin and record and record.referred and not record.resolved:
                rows.append([("✅ بستن ارجاع", f"ed:resolve:{classroom_id}:{attendance.id}")])
            status = "رسیدگی‌شده" if record and record.resolved else "ارجاع به مدیریت" if record and record.referred else "بدون ارجاع"
            detail_scope = (user_id, classroom_id, "attendance", 0 if admin else student.id)
            self.education_say(session, chat_id, f"{student.full_name} | {attendance.jalali_date}"
                               f"\nعلت: {record.reason if record else 'ثبت نشده'}\nوضعیت: {status}",
                               rows + [[("بازگشت", back)]], detail_scope)

    def education_roll_page(self, session, chat_id, user_id, state, roster, page):
        data = state.data
        nonce, statuses = data["nonce"], data["statuses"]
        start = page * ROLL_SIZE
        if start >= len(roster):
            return
        rows = []
        for student in roster[start:start + ROLL_SIZE]:
            current = statuses.get(str(student.id), "انتخاب نشده")
            rows.append([(f"{student.full_name} | {current}", f"ed:rollmark:{nonce}:{student.id}:{page}")])
        nav = []
        if page:
            nav.append(("قبلی", f"ed:rollpage:{nonce}:{page - 1}"))
        if start + ROLL_SIZE < len(roster):
            nav.append(("بعدی", f"ed:rollpage:{nonce}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.extend([[("باقی‌مانده‌ها حاضر", f"ed:rollall:{nonce}:{page}")],
                     [("مرور و تأیید", f"ed:rollreview:{nonce}"), ("لغو", f"ed:cancel:{nonce}")]])
        from .education import jalali_day

        jalali = jalali_day(date.fromisoformat(data["session_date"]))
        self.education_say(session, chat_id, f"حضور و غیاب {jalali} | {len(statuses)} از {len(roster)}"
                           "\nبا زدن نام، وضعیت بین حاضر و غایب تغییر می‌کند.", rows,
                           (user_id, data["classroom_id"], "attendance", 0))

    def education_roll_action(self, session, chat_id, user_id, state, action, args):
        data = state.data
        roster = members(session, data["classroom_id"])
        if sorted(student.id for student in roster) != data["roster"]:
            self.set_state(state)
            self.say(session, chat_id, "فهرست اعضا تغییر کرده است؛ ثبت را دوباره شروع کنید.")
            return
        statuses = dict(data["statuses"])
        if action == "rollreview" and not args:
            if len(statuses) != len(roster):
                self.say(session, chat_id, "برای همه دانش‌آموزان وضعیت انتخاب کنید یا «باقی‌مانده‌ها حاضر» را بزنید.")
                return
            present = sum(value == "حاضر" for value in statuses.values())
            self.education_preview(session, chat_id, state, "roll",
                                   f"حاضر: {present} | غایب: {len(roster) - present}\n"
                                   "ثبت نهایی در سوابق حضور و غیاب و صف ارسال اعلان تأیید شود؟",
                                   f"ed:attendance:{data['classroom_id']}:0", classroom_id=data["classroom_id"],
                                   session_date=data["session_date"], roster=data["roster"], statuses=statuses,
                                   attendance_version=_attendance_version(session, data))
            return
        if action == "rollreview":
            return
        values = identifiers(args, ID_MAX, ID_MAX) if action == "rollmark" else identifiers(args, ID_MAX)
        if values is None:
            return
        page = values[-1]
        if page * ROLL_SIZE >= len(roster):
            return
        if action == "rollmark":
            student_id = values[0]
            if student_id not in {student.id for student in roster[page * ROLL_SIZE:(page + 1) * ROLL_SIZE]}:
                return
            key = str(student_id)
            statuses[key] = "غایب" if statuses.get(key) == "حاضر" else "حاضر"
        elif action == "rollall":
            for student in roster:
                statuses.setdefault(str(student.id), "حاضر")
        self.set_state(state, **{**data, "statuses": statuses})
        self.education_roll_page(session, chat_id, user_id, state, roster, page)

    def education_followup_form(self, session, chat_id, state, data, text):
        fields = [field.strip() for field in text.splitlines()]
        if len(fields) != 2 or not 1 <= len(fields[0]) <= 600 or fields[1] not in ("ارجاع", "بدون ارجاع"):
            self.say(session, chat_id, "علت (حداکثر ۶۰۰ نویسه) و «ارجاع» یا «بدون ارجاع» را در دو خط بفرستید.")
            return
        classroom_id, attendance_id = data["classroom_id"], data["attendance_id"]
        self.education_preview(session, chat_id, state, "followup", f"علت: {fields[0]}\nپیگیری: {fields[1]}\nثبت شود؟",
                               f"ed:absenceview:{classroom_id}:{attendance_id}", classroom_id=classroom_id,
                               attendance_id=attendance_id, reason=fields[0], referred=fields[1] == "ارجاع")

    def education_attendance_commit(self, session, chat_id, user_id, data):
        classroom_id = data["classroom_id"]
        if data["operation"] == "roll":
            roster = members(session, classroom_id)
            if sorted(student.id for student in roster) != data["roster"] or _attendance_version(session, data) != data["attendance_version"]:
                self.say(session, chat_id, "فهرست اعضا یا سوابق حضور تغییر کرده؛ دوباره وضعیت‌ها را ثبت کنید.")
                return False
            apply_import(session, {"kind": "attendance", "classroom_id": classroom_id, "session_date": data["session_date"],
                         "rows": [{"national_id": student.national_id, "name": student.full_name,
                                   "status": data["statuses"][str(student.id)]} for student in roster]})
            return True
        admin = is_admin(session, user_id, self.config.admin_id)
        attendance = session.get(Attendance, data["attendance_id"])
        if attendance is None or attendance.classroom_id != classroom_id or attendance.status != "غایب" or (
                not admin and active_member(session, classroom_id, attendance.student_id) is None):
            self.say(session, chat_id, "غیبت موردنظر دیگر قابل پیگیری نیست.")
            return False
        record = session.get(AbsenceFollowup, attendance.id)
        if data["operation"] == "resolve":
            if not admin or record is None or not record.referred or record.resolved:
                self.say(session, chat_id, "ارجاع قابل بستن نیست.")
                return False
            record.resolved = True
            return True
        if record is None:
            session.add(AbsenceFollowup(attendance_id=attendance.id, reason=data["reason"], actor_id=user_id,
                                        referred=data["referred"]))
        else:
            record.reason = data["reason"]
            record.actor_id = user_id
            if admin or data["referred"]:
                record.referred = data["referred"]
                record.resolved = False
        return True
