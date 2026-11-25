import base64
import hashlib
import secrets
from datetime import timedelta

from . import excel
from .api import BaleError
from .education_access import MENTOR_PANEL
from .education_models import Mentor, MentorRegistration
from .models import LoginThrottle, utcnow


def _digest(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class MentorRegistrationMixin:
    def mentor_template(self, session, chat_id, state):
        if not self.allow(session, chat_id, ["excel"]):
            return
        self.set_state(state, mode="mentor_upload")
        self.meeting_document(session, chat_id, excel.workbook("mentors"), "mentor-template.xlsx",
                              "نام، کد ملی و شماره تماس مربیان را وارد و فایل را بفرستید.")

    def mentor_upload(self, session, chat_id, state, message):
        if not self.allow(session, chat_id, ["excel"]):
            return
        document = message.get("document")
        if not isinstance(document, dict):
            self.say(session, chat_id, "فایل نمونهٔ تکمیل‌شده را بفرستید.")
            return
        try:
            rows = excel.parse(self.api.download(document), "mentors", None, self.config.max_excel_rows)["rows"]
            if any(len(row["name"]) > 100 for row in rows):
                raise ValueError("نام مربی حداکثر ۱۰۰ نویسه است.")
        except BaleError as error:
            if error.code not in (400, 403, 404):
                raise
            self.say(session, chat_id, "دانلود فایل انجام نشد.")
            return
        except ValueError as error:
            self.say(session, chat_id, f"فایل پذیرفته نشد: {error}")
            return
        nonce = secrets.token_hex(8)
        self.set_state(state, mode="mentor_import", nonce=nonce, rows=rows)
        self.education_say(session, chat_id, f"ثبت {len(rows)} مربی تأیید شود؟ برای حساب‌های فعال‌نشده کد جدید صادر می‌شود.",
                           [[("تأیید ثبت", f"mi:{nonce}"), ("لغو", "cancel")]])

    def mentor_import(self, session, chat_id, user_id, state, nonce):
        pending = state.data
        if not self.allow(session, chat_id, ["excel"]):
            return
        if pending.get("mode") != "mentor_import" or pending.get("nonce") != nonce:
            self.say(session, chat_id, "این درخواست معتبر نیست یا قبلاً ثبت شده است.")
            return
        self.set_state(state)
        credentials = []
        for row in pending["rows"]:
            registration = session.get(MentorRegistration, row["national_id"])
            if registration is not None and registration.user_id is not None:
                continue
            if registration is None:
                registration = MentorRegistration(national_id=row["national_id"])
                session.add(registration)
            code = secrets.token_hex(8)
            registration.name, registration.phone = row["name"], row["phone"]
            registration.activation_hash = _digest(code)
            credentials.append((row["name"], row["national_id"], row["phone"], code))
        self.audit(session, user_id, "mentor_excel_import", count=len(credentials))
        if credentials:
            content = excel.workbook("mentor_codes", credentials)
            self.queue(session, "sendDocument", {"chat_id": chat_id, "caption":
                       "محرمانه: فقط کد مربوط به هر مربی را برای خودش بفرستید. مربی با /mentor وارد می‌شود.",
                       "_file": {"name": "mentor-activation.xlsx", "content": base64.b64encode(content).decode("ascii")}})
        self.say(session, chat_id, f"ثبت یا صدور کد جدید: {len(credentials)} | حساب‌های قبلاً فعال: {len(pending['rows']) - len(credentials)}")

    def mentor_login(self, session, chat_id, user_id, state, text):
        now = utcnow()
        throttle = session.get(LoginThrottle, user_id)
        if throttle is None:
            throttle = LoginThrottle(user_id=user_id, attempts=0, window_start=now)
            session.add(throttle)
        if now - throttle.window_start.replace(tzinfo=now.tzinfo) > timedelta(minutes=15):
            throttle.attempts, throttle.window_start = 0, now
        if throttle.attempts >= 6:
            self.say(session, chat_id, "تعداد تلاش‌ها بیش از حد مجاز است. ۱۵ دقیقه بعد دوباره تلاش کنید.")
            return
        throttle.attempts += 1
        fields = text.split()
        registration = None
        if len(fields) == 3 and len(fields[2]) == 16:
            try:
                registration = session.get(MentorRegistration, excel.national_id(fields[0]))
            except ValueError:
                pass
        if (registration is None or registration.user_id is not None or not registration.activation_hash
                or registration.phone != excel.normalize(fields[1])
                or not secrets.compare_digest(registration.activation_hash, _digest(fields[2].lower()))):
            self.say(session, chat_id, "اطلاعات ورود معتبر نیست. کد ملی، شماره تماس و کد فعال‌سازی را بررسی کنید.")
            return
        from sqlalchemy import select

        occupied = session.scalar(select(MentorRegistration.national_id).where(MentorRegistration.user_id == user_id))
        mentor = session.get(Mentor, user_id)
        if occupied or (mentor is not None and not mentor.active):
            self.say(session, chat_id, "این حساب قابل اتصال نیست؛ با مدیریت تماس بگیرید.")
            return
        if mentor is None:
            session.add(Mentor(user_id=user_id, name=registration.name))
            session.flush()
        registration.user_id, registration.activation_hash = user_id, None
        session.delete(throttle)
        self.set_state(state)
        self.audit(session, user_id, "mentor_activate")
        self.say(session, chat_id, f"🌱 {registration.name} عزیز، به پنل مربیان خوش آمدید. کلاس‌ها پس از تخصیص ادمین نمایش داده می‌شوند.",
                 {"keyboard": [[{"text": MENTOR_PANEL}]], "resize_keyboard": True})
