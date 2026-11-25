import secrets
from datetime import date

import jdatetime
from sqlalchemy import select

from . import excel
from .education_access import (
    ID_MAX, PERMISSIONS, active_member, class_access, class_students, family_recipients, identifiers, mentor_active,
)
from .education_admin import ADMIN_ROUTES, EducationAdminMixin
from .education_attendance import ATTENDANCE_ROUTES, EducationAttendanceMixin
from .education_homework import HOMEWORK_ROUTES, EducationHomeworkMixin
from .education_models import FamilyMessage, GuardianAccount, GuardianLink, MentorClass, SessionReport
from .features import enabled
from .models import Classroom, Outbox, Student


PAGE_SIZE = 8
ADMIN_OPERATIONS = {"mentor_add", "assign", "guardian_add", "guardian_remove"}
ROUTES = {
    "class": (None, 1), "roster": ("roster", 2), "student": ("roster", 2),
    "reports": ("reports", 2), "report": ("reports", 2), "reportnew": ("reports", 1),
    "family": ("family", 2), "familymsg": ("family", 2), "familycontact": ("family", 2),
    **ATTENDANCE_ROUTES, **HOMEWORK_ROUTES,
}
OPERATION_FEATURE = {
    "report": "reports", "family": "family", "roll": "attendance",
    "followup": "attendance", "resolve": "attendance", "homework": "homework", "feedback": "homework",
}


def jalali_day(value):
    return jdatetime.date.fromgregorian(date=value).strftime("%Y/%m/%d")


def education_date(text):
    fields = excel.normalize(text).split("/")
    result = identifiers(fields, 1600, 12, 31)
    if result is None or len(fields[0]) != 4 or not 1300 <= result[0] <= 1600:
        raise ValueError
    return jdatetime.date(*result).togregorian()


class EducationMixin(EducationAdminMixin, EducationAttendanceMixin, EducationHomeworkMixin):
    def education_say(self, session, chat_id, text, rows=None, scope=None):
        markup = None if rows is None else {"inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in rows
        ]}
        if scope is None:
            self.say(session, chat_id, text, markup)
        else:
            actor_id, classroom_id, feature, student_id = scope
            for offset in range(0, len(text), 3500):
                payload = {"chat_id": chat_id, "text": text[offset:offset + 3500]}
                if markup is not None and offset + 3500 >= len(text):
                    payload["reply_markup"] = markup
                session.add(Outbox(method="sendMessage", payload=payload,
                                   dedupe_key=f"edview:{actor_id}:{classroom_id}:{feature or ''}:{student_id}:{secrets.token_hex(12)}"))

    @staticmethod
    def _education_scope(state, operation, fields):
        feature = OPERATION_FEATURE.get(operation)
        if feature is not None:
            return state.user_id, fields["classroom_id"], feature, fields.get("student_id") or 0
        return None

    def education_page(self, session, chat_id, query, page, prefix, heading, button, controls=(), scope=None):
        records = list(session.scalars(query.offset(page * PAGE_SIZE).limit(PAGE_SIZE + 1)))
        rows = [[button(record)] for record in records[:PAGE_SIZE]]
        navigation = []
        if page:
            navigation.append(("قبلی", f"{prefix}:{page - 1}"))
        if len(records) > PAGE_SIZE:
            navigation.append(("بعدی", f"{prefix}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.extend([[control] for control in controls])
        self.education_say(session, chat_id, heading if records else f"{heading}\nموردی برای نمایش نیست.", rows or None, scope)

    def education_form(self, session, chat_id, state, operation, prompt, **fields):
        self.set_state(state, mode="education_form", operation=operation, **fields)
        self.education_say(session, chat_id, prompt, [[("لغو", "ed:cancel")]], self._education_scope(state, operation, fields))

    def education_preview(self, session, chat_id, state, operation, prompt, back, **fields):
        nonce = secrets.token_hex(8)
        self.set_state(state, mode="education_confirm", operation=operation, nonce=nonce, back=back, **fields)
        self.education_say(session, chat_id, prompt, [[("تأیید", f"ed:confirm:{nonce}"), ("لغو", f"ed:cancel:{nonce}")]],
                           self._education_scope(state, operation, fields))

    def education_guardian_command(self, session, chat_id, user_id, subscribe):
        account = session.get(GuardianAccount, user_id)
        link = session.scalar(select(GuardianLink.student_id).where(GuardianLink.guardian_id == user_id).limit(1))
        if account is None or link is None:
            self.say(session, chat_id, "حساب شما به‌عنوان ولی ثبت نشده است. شناسه /id را به مدیریت بدهید.")
            return
        account.chat_id = chat_id if subscribe else None
        if subscribe:
            self.education_say(session, chat_id, "دریافت پیام خانواده فعال شد.",
                               [[("📅 جلسات والدین", "pf:list:0")]])
        else:
            self.say(session, chat_id, "دریافت پیام خانواده متوقف شد.")

    def education_classes(self, session, chat_id, user_id, state, admin, page=0):
        self.set_state(state)
        query = select(Classroom).order_by(Classroom.id)
        if not admin:
            query = query.join(MentorClass).where(MentorClass.mentor_id == user_id)
        self.education_page(session, chat_id, query, page, "ed:classes", "🎓 کلاس‌های پنل آموزشی",
                            lambda classroom: (classroom.title, f"ed:class:{classroom.id}"),
                            [("مدیریت مربیان", "ed:manage:0")] if admin else [])

    def education_allowed(self, session, chat_id, user_id, classroom_id, feature):
        allowed = class_access(session, self.config.admin_id, user_id, classroom_id, feature)
        if allowed and feature == "attendance":
            allowed = enabled(session, "attendance")
        if not allowed:
            self.say(session, chat_id, "دسترسی به این کلاس یا امکان مجاز نیست.")
        return allowed

    def education_state_access(self, session, chat_id, user_id, admin, data):
        operation = data.get("operation")
        if operation in ADMIN_OPERATIONS:
            if not admin:
                self.say(session, chat_id, "فقط ادمین به این عملیات دسترسی دارد.")
            return admin
        if operation in OPERATION_FEATURE:
            return self.education_allowed(session, chat_id, user_id, data["classroom_id"], OPERATION_FEATURE[operation])
        return False

    def education_callback(self, session, chat_id, user_id, state, admin, payload):
        _, action, *args = payload.split(":")
        if not (admin or mentor_active(session, user_id)):
            self.say(session, chat_id, "دسترسی به پنل آموزشی مجاز نیست.")
            return
        if action in ADMIN_ROUTES:
            if not admin:
                self.say(session, chat_id, "فقط ادمین به این عملیات دسترسی دارد.")
                return
            self.education_admin_action(session, chat_id, user_id, state, action, args)
            return
        if action == "classes":
            values = identifiers(args, ID_MAX)
            if values is not None:
                self.education_classes(session, chat_id, user_id, state, admin, values[0])
            return
        if action == "cancel":
            if state.data.get("mode", "").startswith("education_") and (
                    args == [state.data.get("nonce")] or (not args and "nonce" not in state.data)):
                self.set_state(state)
                self.say(session, chat_id, "عملیات لغو شد.")
            return
        if action in ("confirm", "rollpage", "rollmark", "rollall", "rollreview"):
            pending = state.data
            expected = "education_confirm" if action == "confirm" else "education_roll"
            if not args or pending.get("mode") != expected or args[0] != pending.get("nonce"):
                self.say(session, chat_id, "این درخواست منقضی شده یا قبلاً انجام شده است.")
                return
            if not self.education_state_access(session, chat_id, user_id, admin, pending):
                self.set_state(state)
                return
            if action == "confirm" and len(args) == 1:
                self.set_state(state)
                self.education_commit(session, chat_id, user_id, state, pending)
            elif action != "confirm":
                self.education_roll_action(session, chat_id, user_id, state, action, args[1:])
            return
        if action not in ROUTES:
            return
        feature, count = ROUTES[action]
        values = identifiers(args, *([ID_MAX] * count))
        if values is None or not self.education_allowed(session, chat_id, user_id, values[0], feature):
            return
        self.set_state(state)
        classroom = session.get(Classroom, values[0])
        classroom_id = classroom.id
        back = f"ed:class:{classroom_id}"
        scope = (user_id, classroom_id, feature, 0)
        if action == "class":
            routes = {"roster": "roster", "reports": "reports", "attendance": "attendance",
                      "homework": "homework", "family": "family", "coins": "coins"}
            rows = [[(label, f"ed:{routes[key]}:{classroom_id}:0" if key != "coins" else f"coins:categories:{classroom_id}:0")]
                    for key, (_, label) in PERMISSIONS.items()
                    if class_access(session, self.config.admin_id, user_id, classroom_id, key)
                    and (key not in ("attendance", "coins") or enabled(session, key))]
            if admin:
                rows.extend([[("مدیریت اولیا", f"ed:guardians:{classroom_id}:0")],
                             [("سوابق پیام خانواده", f"ed:familylog:{classroom_id}:0")]])
            self.education_say(session, chat_id, f"🎓 کلاس «{classroom.title}»"
                               + ("" if rows else "\nهنوز مجوزی برای این کلاس فعال نیست."),
                               rows + [[("بازگشت به کلاس‌ها", "ed:classes:0")]], scope)
        elif action in ("roster", "family"):
            self.education_page(session, chat_id, class_students(classroom_id).order_by(Student.full_name, Student.id),
                                values[1], f"ed:{action}:{classroom_id}",
                                "فهرست اعضای فعال" if action == "roster" else "انتخاب مخاطب خانواده",
                                lambda student: (student.full_name,
                                                 f"ed:{'student' if action == 'roster' else 'familycontact'}:{classroom_id}:{student.id}"),
                                ([("پیام گروهی به اولیای همین کلاس", f"ed:familymsg:{classroom_id}:0")]
                                 if action == "family" else []) + [("بازگشت", back)], scope)
        elif action == "student":
            student = active_member(session, classroom_id, values[1])
            if student is None:
                self.say(session, chat_id, "دانش‌آموز عضو فعال این کلاس نیست.")
                return
            contact = "متصل" if student.bale_user_id is not None else "متصل‌نشده"
            self.education_say(session, chat_id, f"نام: {student.full_name}\nکلاس: {classroom.title}"
                               f"\nشناسه داخلی: {student.id}\nحساب بات: {contact}",
                               [[("بازگشت", f"ed:roster:{classroom_id}:0")]],
                               (user_id, classroom_id, feature, student.id))
        elif action in ATTENDANCE_ROUTES:
            self.education_attendance_action(session, chat_id, user_id, state, admin, classroom, action, values)
        elif action in HOMEWORK_ROUTES:
            self.education_homework_action(session, chat_id, user_id, state, classroom, action, values)
        elif action == "reports":
            self.education_page(session, chat_id, select(SessionReport).where(
                SessionReport.classroom_id == classroom_id,
            ).order_by(SessionReport.id.desc()), values[1], f"ed:reports:{classroom_id}", "گزارش‌های جلسه",
                lambda report: (f"{jalali_day(report.session_date)} | {report.topic}", f"ed:report:{classroom_id}:{report.id}"),
                [("➕ ثبت گزارش", f"ed:reportnew:{classroom_id}"), ("بازگشت", back)], scope)
        elif action == "report":
            report = session.get(SessionReport, values[1])
            if report is not None and report.classroom_id == classroom_id:
                self.education_say(session, chat_id, f"تاریخ جلسه: {jalali_day(report.session_date)}"
                                   f"\nموضوع: {report.topic}\nحضور: {report.presence}\nفعالیت: {report.activity}"
                                   f"\nاقدام بعدی: {report.next_action}",
                                   [[("بازگشت", f"ed:reports:{classroom_id}:0")]], scope)
            else:
                self.say(session, chat_id, "گزارش در این کلاس پیدا نشد.")
        elif action == "reportnew":
            self.education_form(session, chat_id, state, "report",
                                "گزارش امروز را در چهار خط بفرستید:\nموضوع\nخلاصه حضور\nفعالیت\nاقدام بعدی",
                                classroom_id=classroom_id, session_date=excel.today()[0].isoformat())
        elif action == "familymsg":
            student_id = values[1] or None
            if student_id is not None and not active_member(session, classroom_id, student_id):
                self.say(session, chat_id, "دانش‌آموز عضو فعال این کلاس نیست.")
                return
            self.education_form(session, chat_id, state, "family", "متن پیام به اولیا را بفرستید (حداکثر ۱۵۰۰ نویسه).",
                                classroom_id=classroom_id, student_id=student_id)
        elif action == "familycontact":
            student = active_member(session, classroom_id, values[1])
            if student is None:
                self.say(session, chat_id, "دانش‌آموز عضو فعال این کلاس نیست.")
                return
            self.education_say(session, chat_id, f"اطلاعات تماس خانوادهٔ «{student.full_name}»"
                               f"\nپدر: {student.father_phone or 'ثبت نشده'}\nمادر: {student.mother_phone or 'ثبت نشده'}",
                               [[("ارسال پیام در بله", f"ed:familymsg:{classroom_id}:{student.id}")],
                                [("بازگشت", f"ed:family:{classroom_id}:0")]],
                               (user_id, classroom_id, "family", student.id))

    def education_message(self, session, chat_id, user_id, state, admin, text):
        data = state.data
        if not self.education_state_access(session, chat_id, user_id, admin, data):
            self.set_state(state)
            return
        if data["mode"] != "education_form":
            self.say(session, chat_id, "از دکمه‌های مرحلهٔ فعلی استفاده کنید یا /cancel را بفرستید.")
            return
        if not text or len(text) > 3500 or any(ord(char) < 32 and char not in "\n\r\t" for char in text):
            self.say(session, chat_id, "متن معتبر را در یک پیام بفرستید.")
            return
        operation = data["operation"]
        if operation in ADMIN_OPERATIONS:
            self.education_admin_form(session, chat_id, state, data, text)
            return
        classroom_id = data["classroom_id"]
        if operation == "report":
            fields = [line.strip() for line in text.splitlines()]
            if len(fields) != 4 or not all(0 < len(value) <= limit for value, limit in zip(fields, (200, 300, 600, 600))):
                self.say(session, chat_id, "چهار خط غیرخالی بفرستید؛ سقف نویسه به‌ترتیب ۲۰۰، ۳۰۰، ۶۰۰، ۶۰۰ است.")
                return
            self.education_preview(session, chat_id, state, operation, "گزارش جلسه ثبت شود؟\n" + "\n".join(fields),
                                   f"ed:reports:{classroom_id}:0", classroom_id=classroom_id,
                                   session_date=data["session_date"], report_fields=fields)
        elif operation == "family":
            self.education_family_preview(session, chat_id, state, data, text)
        elif operation == "followup":
            self.education_followup_form(session, chat_id, state, data, text)
        else:
            self.education_homework_form(session, chat_id, state, data, text)

    def education_commit(self, session, chat_id, user_id, state, data):
        operation = data["operation"]
        if operation in ADMIN_OPERATIONS:
            self.education_admin_commit(session, chat_id, user_id, state, data)
            return
        classroom_id = data["classroom_id"]
        if operation == "report":
            topic, presence, activity, next_action = data["report_fields"]
            session.add(SessionReport(classroom_id=classroom_id, actor_id=user_id,
                                      session_date=date.fromisoformat(data["session_date"]),
                                      topic=topic, presence=presence, activity=activity, next_action=next_action))
        elif operation == "family":
            if not self.education_family_commit(session, chat_id, user_id, data):
                return
        elif operation in ("roll", "followup", "resolve"):
            if not self.education_attendance_commit(session, chat_id, user_id, data):
                return
        elif not self.education_homework_commit(session, chat_id, user_id, data):
            return
        self.audit(session, user_id, f"education_{operation}", classroom_id=classroom_id)
        self.education_say(session, chat_id, "✅ ثبت شد." if operation != "family" else "✅ پیام در صف ارسال قرار گرفت.",
                           [[("بازگشت", data["back"])]])

    def education_family_preview(self, session, chat_id, state, data, text):
        if len(text) > 1500:
            self.say(session, chat_id, "پیام حداکثر ۱۵۰۰ نویسه باشد.")
            return
        classroom_id, student_id = data["classroom_id"], data["student_id"]
        recipients = family_recipients(session, classroom_id, student_id)
        if not recipients:
            self.say(session, chat_id, "حساب ولیِ فعال برای این انتخاب پیدا نشد.")
            return
        classroom = session.get(Classroom, classroom_id)
        student = active_member(session, classroom_id, student_id) if student_id is not None else None
        target = student.full_name if student else "همهٔ اولیای مرتبط با کلاس"
        self.education_preview(session, chat_id, state, "family",
                               f"ارسال به {len(recipients)} حساب ولی تأیید شود؟"
                               f"\nکلاس: {classroom.title}\nمخاطب: {target}\n\n{text}",
                               f"ed:family:{classroom_id}:0", classroom_id=classroom_id, student_id=student_id,
                               recipients=sorted(item.user_id for item in recipients), body=text)

    def education_family_commit(self, session, chat_id, user_id, data):
        classroom_id, student_id = data["classroom_id"], data["student_id"]
        recipients = family_recipients(session, classroom_id, student_id)
        if not recipients or sorted(item.user_id for item in recipients) != data["recipients"]:
            self.say(session, chat_id, "فهرست مخاطبان تغییر کرده است؛ پیام را دوباره پیش‌نمایش کنید.")
            return False
        classroom = session.get(Classroom, classroom_id)
        message = FamilyMessage(classroom_id=classroom_id, student_id=student_id, actor_id=user_id, body=data["body"])
        session.add(message)
        session.flush()
        for recipient_id, recipient_chat in recipients:
            session.add(Outbox(method="sendMessage", dedupe_key=f"edfamily:{message.id}:{recipient_id}",
                               payload={"chat_id": recipient_chat, "text": f"✉️ پیام آموزشی کلاس «{classroom.title}»\n\n{message.body}"}))
        return True

    def education_family_history(self, session, chat_id, classroom_id, action, value):
        if action == "familylog":
            self.education_page(session, chat_id, select(FamilyMessage).where(
                FamilyMessage.classroom_id == classroom_id,
            ).order_by(FamilyMessage.id.desc()), value, f"ed:familylog:{classroom_id}", "سوابق پیام خانواده",
                lambda message: (f"{'گروهی' if message.student_id is None else 'فردی'} | {message.body[:35]}",
                                 f"ed:familyview:{classroom_id}:{message.id}"), [("بازگشت", f"ed:class:{classroom_id}")])
            return
        message = session.get(FamilyMessage, value)
        if message is None or message.classroom_id != classroom_id:
            self.say(session, chat_id, "پیام در این کلاس پیدا نشد.")
            return
        student = session.get(Student, message.student_id) if message.student_id else None
        self.education_say(session, chat_id, f"فرستنده: {message.actor_id}\nمخاطب: "
                           f"{student.full_name if student else 'گروهی'}\n\n{message.body}",
                           [[("بازگشت", f"ed:familylog:{classroom_id}:0")]])
