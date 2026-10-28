import base64
import secrets
from datetime import timedelta

from sqlalchemy import func, select

from . import excel
from .api import BaleError
from .models import (
    Attendance, Audit, Classroom, Conversation, Discount, Enrollment, LoginThrottle, Outbox, Setting, Student, utcnow,
)
from .service import apply_import, members, validate_import


STUDENT_MENU = [["📋 حضور و غیاب من"], ["🎁 تخفیف‌های من", "☎️ راه‌های ارتباطی"]]
ADMIN_MENU = [
    ["🏫 کلاس‌ها", "➕ ایجاد کلاس"],
    ["📢 پیام همگانی", "⚙️ تنظیم راه‌های ارتباطی"],
    ["📊 وضعیت ارسال‌ها", "👤 مدیریت دانش‌آموز"],
    ["☎️ راه‌های ارتباطی", "لغو"],
]


def inline(rows):
    return {"inline_keyboard": [[{"text": title, "callback_data": data} for title, data in row] for row in rows]}


def reply(admin=False):
    return {"keyboard": [[{"text": text} for text in row] for row in (ADMIN_MENU if admin else STUDENT_MENU)],
            "resize_keyboard": True}


class Bot:
    def __init__(self, config, api):
        self.config = config
        self.api = api

    def queue(self, session, method, payload):
        session.add(Outbox(method=method, payload=payload))

    def say(self, session, chat_id, text, markup=None):
        for offset in range(0, len(text), 3500):
            payload = {"chat_id": chat_id, "text": text[offset:offset + 3500]}
            if markup is not None and offset + 3500 >= len(text):
                payload["reply_markup"] = markup
            self.queue(session, "sendMessage", payload)

    def state(self, session, user_id):
        state = session.get(Conversation, user_id)
        if state is None:
            state = Conversation(user_id=user_id, data={})
            session.add(state)
        return state

    def set_state(self, state, **data):
        state.data = data
        state.updated_at = utcnow()

    def audit(self, session, user_id, action, **details):
        session.add(Audit(actor_id=user_id, action=action, details=details))

    def home(self, session, chat_id, admin):
        self.say(session, chat_id, "گزینه موردنظر را انتخاب کنید.", reply(admin))

    def handle(self, session, update):
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        sender = callback.get("from", {}) if callback else message.get("from", {})
        user_id = sender.get("id")
        chat = message.get("chat", {})
        if not user_id or chat.get("type") != "private":
            return
        chat_id = chat["id"]
        admin = user_id == self.config.admin_id
        if admin:
            setting = session.get(Setting, "admin_chat_id")
            if setting:
                setting.value = str(chat_id)
            else:
                session.add(Setting(key="admin_chat_id", value=str(chat_id)))
        state = self.state(session, user_id)
        if state.data and (utcnow() - state.updated_at.replace(tzinfo=utcnow().tzinfo)) > timedelta(hours=2):
            self.set_state(state)
        if callback:
            if not str(callback["id"]).startswith("1"):
                self.queue(session, "answerCallbackQuery", {"callback_query_id": callback["id"]})
            if not admin:
                self.say(session, chat_id, "دسترسی مجاز نیست.")
                return
            self.callback(session, chat_id, user_id, state, callback.get("data", ""))
            return
        text = message.get("text", "").strip()
        if text in ("/start", "/cancel", "لغو", "منوی اصلی"):
            self.set_state(state)
            if admin:
                self.home(session, chat_id, True)
            else:
                student = session.scalar(select(Student).where(Student.bale_user_id == user_id, Student.active.is_(True)))
                if student:
                    self.home(session, chat_id, False)
                else:
                    self.say(session, chat_id, "لطفاً کد ملی ۱۰ رقمی خود را ارسال کنید.", {"remove_keyboard": True})
            return
        if admin:
            self.admin_message(session, chat_id, user_id, state, message, text)
        else:
            self.student_message(session, chat_id, user_id, state, text)

    def student_message(self, session, chat_id, user_id, state, text):
        student = session.scalar(select(Student).where(Student.bale_user_id == user_id, Student.active.is_(True)))
        if student is None:
            throttle = session.get(LoginThrottle, user_id)
            if throttle is None:
                throttle = LoginThrottle(user_id=user_id, attempts=0, window_start=utcnow())
                session.add(throttle)
            if utcnow() - throttle.window_start.replace(tzinfo=utcnow().tzinfo) > timedelta(minutes=15):
                throttle.attempts = 0
                throttle.window_start = utcnow()
            if throttle.attempts >= 10:
                self.say(session, chat_id, "تعداد تلاش‌های ورود بیش از حد مجاز است؛ ۱۵ دقیقه بعد دوباره تلاش کنید.")
                return
            throttle.attempts += 1
            try:
                code = excel.national_id(text)
            except ValueError:
                self.say(session, chat_id, "کد ملی باید دقیقاً ۱۰ رقم باشد.")
                return
            student = session.scalar(select(Student).where(Student.national_id == code, Student.active.is_(True)))
            if student is None:
                self.say(session, chat_id, "کد ملی وارد شده در سامانه موجود نیست؛ لطفاً با مدیریت تماس بگیرید.")
                return
            existing = session.scalar(select(Student).where(Student.bale_user_id == user_id))
            if student.bale_user_id not in (None, user_id) or existing not in (None, student):
                self.say(session, chat_id, "این کد ملی یا حساب قبلاً متصل شده است؛ لطفاً با مدیریت تماس بگیرید.")
                return
            student.bale_user_id = user_id
            student.chat_id = chat_id
            session.delete(throttle)
            self.audit(session, user_id, "student_login", student_id=student.id)
            self.say(session, chat_id, f"{student.full_name} عزیز، خوش آمدید.", reply())
        elif text == "📋 حضور و غیاب من":
            records = session.execute(select(Attendance, Classroom.title).join(
                Classroom, Classroom.id == Attendance.classroom_id,
            ).where(Attendance.student_id == student.id).order_by(
                Attendance.session_date.desc(), Attendance.id.desc(),
            ).limit(30)).all()
            output = "\n\n".join(f"کلاس: {title}\n{item.jalali_date}: {item.status}" for item, title in records)
            self.say(session, chat_id, output or "هنوز حضور و غیابی برای شما ثبت نشده است.")
        elif text == "🎁 تخفیف‌های من":
            discounts = session.execute(select(Discount, Classroom.title).join(
                Classroom, Classroom.id == Discount.classroom_id,
            ).join(Enrollment, (Enrollment.classroom_id == Discount.classroom_id) &
                   (Enrollment.student_id == Discount.student_id)).where(
                Discount.student_id == student.id,
            ).order_by(Discount.id)).all()
            output = "\n\n".join(f"کلاس: {title}\nکد تخفیف: {item.code}\n{item.description}" for item, title in discounts)
            self.say(session, chat_id, output or "تخفیفی برای شما ثبت نشده است.")
        elif text == "☎️ راه‌های ارتباطی":
            self.contacts(session, chat_id)
        else:
            self.home(session, chat_id, False)

    def contacts(self, session, chat_id):
        setting = session.get(Setting, "contacts")
        self.say(session, chat_id, setting.value if setting else self.config.contact_text)

    def classes(self, session, chat_id, page=0):
        classes = list(session.scalars(select(Classroom).order_by(Classroom.id).offset(page * 15).limit(16)))
        rows = [[(item.title, f"class:{item.id}")] for item in classes[:15]]
        navigation = []
        if page:
            navigation.append(("قبلی", f"page:{page - 1}"))
        if len(classes) > 15:
            navigation.append(("بعدی", f"page:{page + 1}"))
        if navigation:
            rows.append(navigation)
        self.say(session, chat_id, "کلاس را انتخاب کنید." if rows else "هنوز کلاسی ایجاد نشده است.", inline(rows))

    def class_menu(self, session, chat_id, classroom):
        count = session.scalar(select(func.count()).select_from(Enrollment).where(Enrollment.classroom_id == classroom.id))
        identifier = classroom.id
        self.say(session, chat_id, f"کلاس: {classroom.title}\nتعداد اعضا: {count}", inline([
            [("ویرایش نام کلاس", f"rename:{identifier}")],
            [("نمونه کاربران", f"template:students:{identifier}"), ("لیست فعلی", f"export:students:{identifier}")],
            [("بارگذاری کاربران", f"upload:students:{identifier}")],
            [("اکسل حضور و غیاب امروز", f"export:attendance:{identifier}")],
            [("بارگذاری حضور و غیاب", f"upload:attendance:{identifier}")],
            [("ثبت حضور و غیاب داخل بات", f"attend:{identifier}")],
            [("اکسل تخفیف‌ها", f"export:discounts:{identifier}")],
            [("بارگذاری تخفیف‌ها", f"upload:discounts:{identifier}")],
            [("ارسال پیام به کلاس", f"broadcast:{identifier}")],
            [("حذف عضویت دانش‌آموز", f"remove:{identifier}")],
        ]))

    def admin_message(self, session, chat_id, user_id, state, message, text):
        if text == "🏫 کلاس‌ها":
            self.set_state(state)
            self.classes(session, chat_id)
            return
        if text == "➕ ایجاد کلاس":
            self.set_state(state, mode="create")
            self.say(session, chat_id, "نام کلاس جدید را بفرستید.")
            return
        if text == "⚙️ تنظیم راه‌های ارتباطی":
            self.set_state(state, mode="contacts")
            self.say(session, chat_id, "متن جدید راه‌های ارتباطی شامل شماره‌ها، آیدی‌ها و لینک‌ها را بفرستید.")
            return
        if text == "☎️ راه‌های ارتباطی":
            self.contacts(session, chat_id)
            return
        if text == "📢 پیام همگانی":
            self.set_state(state, mode="broadcast", classroom_id=None)
            self.say(session, chat_id, "یک پیام متنی یا رسانه‌ای بفرستید. پیش از ارسال نهایی تأیید می‌گیرید.")
            return
        if text == "👤 مدیریت دانش‌آموز":
            self.set_state(state, mode="student_lookup")
            self.say(session, chat_id, "کد ملی دانش‌آموز را بفرستید.")
            return
        if text == "📊 وضعیت ارسال‌ها":
            counts = session.execute(select(Outbox.status, func.count()).group_by(Outbox.status)).all()
            labels = {"pending": "در صف", "sent": "ارسال‌شده", "failed": "ناموفق"}
            self.say(session, chat_id, "وضعیت همه پیام‌ها:\n" + "\n".join(f"{labels.get(status, status)}: {count}" for status, count in counts),
                     inline([[("تلاش مجدد ناموفق‌ها", "retry_failed")]]))
            return
        mode = state.data.get("mode")
        if mode in ("create", "rename"):
            if not text or len(text) > 200:
                self.say(session, chat_id, "نام کلاس باید بین ۱ تا ۲۰۰ کاراکتر باشد.")
                return
            if session.scalar(select(Classroom).where(Classroom.title == text)):
                self.say(session, chat_id, "کلاسی با این نام وجود دارد.")
                return
            if mode == "create":
                classroom = Classroom(title=text)
                session.add(classroom)
                session.flush()
            else:
                classroom = session.get(Classroom, state.data["classroom_id"])
                classroom.title = text
            self.audit(session, user_id, mode + "_class", classroom_id=classroom.id)
            self.set_state(state)
            self.class_menu(session, chat_id, classroom)
        elif mode == "contacts":
            if not text or len(text) > 3500:
                self.say(session, chat_id, "متن باید بین ۱ تا ۳۵۰۰ کاراکتر باشد.")
                return
            setting = session.get(Setting, "contacts")
            if setting:
                setting.value = text
            else:
                session.add(Setting(key="contacts", value=text))
            self.audit(session, user_id, "update_contacts")
            self.set_state(state)
            self.say(session, chat_id, "راه‌های ارتباطی به‌روز شد.")
        elif mode == "upload":
            document = message.get("document")
            if not document:
                self.say(session, chat_id, "فایل xlsx تکمیل‌شده همین کلاس را بفرستید.")
                return
            try:
                content = self.api.download(document)
                pending = excel.parse(content, state.data["kind"], state.data["classroom_id"], self.config.max_excel_rows)
                validate_import(session, pending)
            except BaleError as error:
                if error.code not in (400, 403, 404):
                    raise
                self.say(session, chat_id, "دانلود فایل از بله انجام نشد؛ فایل را مجدداً ارسال کنید.")
                return
            except ValueError as error:
                self.say(session, chat_id, f"فایل ثبت نشد: {error}")
                return
            nonce = secrets.token_hex(8)
            self.set_state(state, mode="confirm_import", nonce=nonce, pending=pending)
            preview = f"تعداد ردیف معتبر: {len(pending['rows'])}\n"
            if pending["kind"] == "attendance":
                import jdatetime
                from datetime import date

                jalali = jdatetime.date.fromgregorian(date=date.fromisoformat(pending["session_date"]))
                present = sum(row["status"] == "حاضر" for row in pending["rows"])
                preview += f"تاریخ جلسه: {jalali.strftime('%Y/%m/%d')}\nحاضر: {present} | غایب: {len(pending['rows']) - present}\n"
            preview += "ثبت نهایی انجام شود؟ رکوردهای موجود به‌روز می‌شوند و اعضای حذف‌شده از فایل، حذف نخواهند شد."
            self.say(session, chat_id, preview, inline([[("تأیید ثبت", f"confirm:{nonce}"), ("لغو", "cancel")]]))
        elif mode == "broadcast":
            if not any(key in message for key in ("text", "photo", "document", "video", "audio", "voice", "animation", "sticker")):
                self.say(session, chat_id, "متن یا رسانه پشتیبانی‌شده ارسال کنید.")
                return
            classroom_id = state.data["classroom_id"]
            count = len(self.recipients(session, classroom_id))
            nonce = secrets.token_hex(8)
            self.set_state(state, mode="confirm_broadcast", nonce=nonce, classroom_id=classroom_id,
                           source_chat_id=chat_id, source_message_id=message["message_id"])
            self.say(session, chat_id, f"پیام به {count} دانش‌آموز فعال و متصل ارسال شود؟",
                     inline([[("تأیید ارسال", f"confirm:{nonce}"), ("لغو", "cancel")]]))
        elif mode in ("student_lookup", "remove"):
            try:
                code = excel.national_id(text)
            except ValueError as error:
                self.say(session, chat_id, str(error))
                return
            student = session.scalar(select(Student).where(Student.national_id == code))
            if not student:
                self.say(session, chat_id, "دانش‌آموز یافت نشد.")
                return
            if mode == "remove":
                enrollment = session.get(Enrollment, (state.data["classroom_id"], student.id))
                if not enrollment:
                    self.say(session, chat_id, "دانش‌آموز عضو این کلاس نیست.")
                    return
                nonce = secrets.token_hex(8)
                self.set_state(state, mode="confirm_remove", nonce=nonce, classroom_id=enrollment.classroom_id, student_id=student.id)
                self.say(session, chat_id, f"عضویت {student.full_name} از کلاس حذف شود؟ سوابق حفظ می‌شوند.",
                         inline([[("حذف عضویت", f"confirm:{nonce}"), ("لغو", "cancel")]]))
            else:
                self.set_state(state)
                self.say(session, chat_id, f"{student.full_name}\nکد ملی: {student.national_id}\nشماره: {student.phone}\nفعال: {'بله' if student.active else 'خیر'}\nمتصل: {'بله' if student.chat_id else 'خیر'}",
                         inline([
                             [("غیرفعال‌سازی" if student.active else "فعال‌سازی", f"toggle:{student.id}")],
                             [("قطع اتصال حساب بله", f"unlink:{student.id}")],
                         ]))
        else:
            self.say(session, chat_id, "از منو گزینه‌ای انتخاب کنید یا «لغو» بفرستید.", reply(True))

    def recipients(self, session, classroom_id):
        statement = select(Student.chat_id).where(Student.active.is_(True), Student.chat_id.is_not(None))
        if classroom_id is not None:
            statement = statement.join(Enrollment).where(Enrollment.classroom_id == classroom_id)
        return list(session.scalars(statement.distinct()))

    def callback(self, session, chat_id, user_id, state, data):
        parts = data.split(":")
        action = parts[0]
        if action == "mark":
            if (len(parts) != 4 or state.data.get("mode") != "attendance"
                    or parts[1] != state.data.get("nonce") or not parts[2].isdigit()
                    or parts[3] not in ("present", "absent")):
                self.say(session, chat_id, "این درخواست منقضی شده است.")
                return
            student_id = int(parts[2])
            roster = members(session, state.data["classroom_id"])
            student = next((item for item in roster if item.id == student_id), None)
            if student is None:
                self.say(session, chat_id, "دانش‌آموز عضو فعال این کلاس نیست.")
                return
            statuses = dict(state.data.get("statuses", {}))
            statuses[str(student_id)] = "حاضر" if parts[3] == "present" else "غایب"
            self.set_state(state, **{**state.data, "statuses": statuses})
            self.attendance_prompt(session, chat_id, state)
            return
        if action == "cancel":
            self.set_state(state)
            self.home(session, chat_id, True)
            return
        if action == "confirm":
            if len(parts) != 2 or parts[1] != state.data.get("nonce"):
                self.say(session, chat_id, "این درخواست منقضی شده یا قبلاً انجام شده است.")
                return
            mode = state.data.get("mode")
            if mode == "confirm_import":
                try:
                    count = apply_import(session, state.data["pending"])
                except ValueError as error:
                    self.say(session, chat_id, str(error))
                    self.set_state(state)
                    return
                self.audit(session, user_id, "import_" + state.data["pending"]["kind"],
                           classroom_id=state.data["pending"]["classroom_id"], rows=count)
                self.say(session, chat_id, f"ثبت انجام شد. تعداد رکورد جدید یا به‌روزشده: {count}؛ اعلان‌ها در صف ارسال قرار گرفتند.")
            elif mode == "confirm_broadcast":
                targets = self.recipients(session, state.data["classroom_id"])
                for target in targets:
                    session.add(Outbox(method="copyMessage", payload={
                        "chat_id": target, "from_chat_id": state.data["source_chat_id"],
                        "message_id": state.data["source_message_id"],
                    }, dedupe_key=f"broadcast:{parts[1]}:{target}"))
                self.audit(session, user_id, "broadcast", classroom_id=state.data["classroom_id"], recipients=len(targets))
                self.say(session, chat_id, f"پیام برای {len(targets)} نفر در صف قرار گرفت. نتیجه از «وضعیت ارسال‌ها» قابل مشاهده است.")
            elif mode == "confirm_remove":
                enrollment = session.get(Enrollment, (state.data["classroom_id"], state.data["student_id"]))
                if enrollment:
                    session.delete(enrollment)
                self.audit(session, user_id, "remove_enrollment", classroom_id=state.data["classroom_id"], student_id=state.data["student_id"])
                self.say(session, chat_id, "عضویت حذف شد؛ سوابق حضور و غیاب و تخفیف‌ها حفظ شدند.")
            elif mode == "confirm_student":
                student = session.get(Student, state.data["student_id"])
                if state.data["operation"] == "unlink":
                    student.bale_user_id = None
                    student.chat_id = None
                else:
                    student.active = not student.active
                self.audit(session, user_id, state.data["operation"] + "_student", student_id=student.id)
                self.say(session, chat_id, "تغییر انجام شد.")
            elif mode == "confirm_retry":
                failed = list(session.scalars(select(Outbox).where(Outbox.status == "failed", Outbox.method != "answerCallbackQuery")))
                for item in failed:
                    item.status = "pending"
                    item.attempts = 0
                    item.available_at = utcnow()
                self.audit(session, user_id, "retry_failed", count=len(failed))
                self.say(session, chat_id, f"{len(failed)} پیام مجدداً در صف قرار گرفت.")
            self.set_state(state)
            return
        if action == "retry_failed":
            nonce = secrets.token_hex(8)
            self.set_state(state, mode="confirm_retry", nonce=nonce)
            self.say(session, chat_id, "پیام‌های ناموفق دوباره ارسال شوند؟ خطاهای دائمی ممکن است دوباره شکست بخورند.",
                     inline([[("تأیید", f"confirm:{nonce}"), ("لغو", "cancel")]]))
            return
        if len(parts) == 2 and parts[1].isdigit():
            identifier = int(parts[1])
            if action == "page":
                self.set_state(state)
                self.classes(session, chat_id, identifier)
                return
            if action in ("toggle", "unlink"):
                student = session.get(Student, identifier)
                if student:
                    nonce = secrets.token_hex(8)
                    self.set_state(state, mode="confirm_student", nonce=nonce, student_id=identifier, operation=action)
                    self.say(session, chat_id, f"تغییر وضعیت یا اتصال {student.full_name} تأیید شود؟",
                             inline([[("تأیید", f"confirm:{nonce}"), ("لغو", "cancel")]]))
                return
            classroom = session.get(Classroom, identifier)
            if classroom is None:
                self.say(session, chat_id, "کلاس یافت نشد.")
                return
            if action == "attend":
                self.set_state(state, mode="attendance", classroom_id=identifier,
                               session_date=excel.today()[0].isoformat(), statuses={}, nonce=secrets.token_hex(8))
                self.attendance_prompt(session, chat_id, state)
            elif action == "class":
                self.set_state(state)
                self.class_menu(session, chat_id, classroom)
            elif action in ("rename", "remove", "broadcast"):
                self.set_state(state, mode=action, classroom_id=identifier)
                prompts = {"rename": "نام جدید کلاس را بفرستید.", "remove": "کد ملی دانش‌آموز را بفرستید.",
                           "broadcast": "پیام متنی یا رسانه‌ای موردنظر برای این کلاس را بفرستید."}
                self.say(session, chat_id, prompts[action])
            return
        if len(parts) == 3 and parts[1] in excel.HEADERS and parts[2].isdigit():
            kind, classroom_id = parts[1], int(parts[2])
            if session.get(Classroom, classroom_id) is None:
                self.say(session, chat_id, "کلاس یافت نشد.")
                return
            if action == "upload":
                self.set_state(state, mode="upload", kind=kind, classroom_id=classroom_id)
                self.say(session, chat_id, "فایل xlsx تکمیل‌شده همین کلاس را ارسال کنید؛ سپس خلاصه و تأیید نمایش داده می‌شود.")
            elif action in ("template", "export"):
                self.export(session, chat_id, kind, classroom_id, action == "template")

    def attendance_prompt(self, session, chat_id, state):
        roster = members(session, state.data["classroom_id"])
        if not roster:
            self.set_state(state)
            self.say(session, chat_id, "این کلاس دانش‌آموز فعالی ندارد.")
            return
        statuses = state.data["statuses"]
        student = next((item for item in roster if str(item.id) not in statuses), None)
        nonce = state.data["nonce"]
        if student:
            self.say(session, chat_id, f"وضعیت {student.full_name} را انتخاب کنید:", inline([
                [("حاضر", f"mark:{nonce}:{student.id}:present"), ("غایب", f"mark:{nonce}:{student.id}:absent")],
                [("لغو", "cancel")],
            ]))
            return
        pending = {"kind": "attendance", "classroom_id": state.data["classroom_id"],
                   "session_date": state.data["session_date"], "rows": [
                       {"national_id": item.national_id, "name": item.full_name, "status": statuses[str(item.id)]}
                       for item in roster]}
        present = sum(row["status"] == "حاضر" for row in pending["rows"])
        self.set_state(state, mode="confirm_import", pending=pending, nonce=nonce)
        self.say(session, chat_id, f"حاضر: {present} | غایب: {len(roster) - present}\nثبت و ارسال اعلان داخل بات تأیید شود؟",
                 inline([[("تأیید ثبت", f"confirm:{nonce}"), ("لغو", "cancel")]]))

    def export(self, session, chat_id, kind, classroom_id, template=False):
        roster = members(session, classroom_id)
        current_date, jalali = excel.today()
        rows = []
        if not template:
            if kind == "students":
                rows = [(student.full_name, student.national_id, student.phone) for student in roster]
            elif kind == "attendance":
                from .models import Attendance

                statuses = {item.student_id: item.status for item in session.scalars(select(Attendance).where(
                    Attendance.classroom_id == classroom_id, Attendance.session_date == current_date,
                ))}
                rows = [(student.national_id, student.full_name, statuses.get(student.id, "")) for student in roster]
            else:
                for student in roster:
                    discounts = list(session.scalars(select(Discount).where(
                        Discount.classroom_id == classroom_id, Discount.student_id == student.id,
                    )))
                    rows.extend((student.national_id, student.full_name, item.code, item.description) for item in discounts)
                    if not discounts:
                        rows.append((student.national_id, student.full_name, "", ""))
        content = excel.workbook(kind, rows, classroom_id, current_date.isoformat() if kind == "attendance" else None)
        self.queue(session, "sendDocument", {
            "chat_id": chat_id,
            "caption": f"کلاس {classroom_id} | {jalali}\nعنوان ستون‌ها و شیت مخفی را تغییر ندهید. کد ملی و شماره تماس را به صورت Text نگه دارید.",
            "_file": {"name": f"{kind}-{classroom_id}-{current_date.isoformat()}.xlsx",
                      "content": base64.b64encode(content).decode("ascii")},
        })
