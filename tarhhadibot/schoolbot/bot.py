import base64
import secrets
from datetime import date, timedelta

import jdatetime
from sqlalchemy import func, select

from . import excel
from .api import BaleError
from .coins import CoinsMixin
from .features import (
    COINS_ADMIN_BUTTON, COINS_STUDENT_BUTTON, FEATURES, TEXT_FEATURES, callback_features, enabled, state_features,
)
from .meetings import MeetingsMixin
from .models import (
    Attendance, Audit, Classroom, Conversation, Discount, Enrollment, LoginThrottle, Outbox, Setting, Student, utcnow,
)
from .preferences import is_admin, records_button, records_title, save_setting
from .service import apply_import, get_pending_absences, members, register_absence, validate_import

REFRESH_BUTTON = "🔄 به‌روزرسانی بات"


def is_refresh(text):
    label = text.lstrip("🔄\ufe0f ").translate(str.maketrans({"ي": "ی", "ك": "ک", "\u200c": None}))
    compact = "".join(label.split())
    return text == "/refresh" or compact in {"بهروزرسانی", "بروزرسانی", "بهروزرسانیبات", "بروزرسانیبات"}

STUDENT_MENU = [
    ["📋 حضور و غیاب من", "🏫 کلاس‌های من"],
    ["📝 ثبت عدم حضور", "🎁 تخفیف‌های من"],
    ["☎️ راه‌های ارتباطی"],
    [COINS_STUDENT_BUTTON],
    ["📅 جلسات من", REFRESH_BUTTON],
]
ADMIN_MENU = [
    ["🏫 کلاس‌ها", "➕ ایجاد کلاس"],
    ["📢 پیام همگانی", "📋 گزارش درخواست‌های عدم حضور"],
    ["📊 وضعیت ارسال‌ها", "👤 مدیریت دانش‌آموز"],
    ["⚙️ امکانات بات", "⚙️ تنظیم راه‌های ارتباطی"],
    ["👥 مدیریت ادمین‌ها", "🎁 نام بخش تخفیف/رکوردها"],
    ["☎️ راه‌های ارتباطی", "لغو"],
    [COINS_ADMIN_BUTTON],
    ["📅 جلسات", REFRESH_BUTTON],
]


def inline(rows):
    return {"inline_keyboard": [[{"text": title, "callback_data": data} for title, data in row] for row in rows]}


def reply(admin=False):
    return {"keyboard": [[{"text": text} for text in row] for row in (ADMIN_MENU if admin else STUDENT_MENU)],
            "resize_keyboard": True}


class Bot(MeetingsMixin, CoinsMixin):
    def __init__(self, config, api):
        self.config = config
        self.api = api

    def queue(self, session, method, payload):
        session.add(Outbox(method=method, payload=payload))

    def say(self, session, chat_id, text, markup=None):
        if markup is None:
            student = session.scalar(select(Student).where(Student.chat_id == chat_id, Student.active.is_(True)))
            if student and not is_admin(session, student.bale_user_id, self.config.admin_id):
                markup = self.menu(session)
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

    def home(self, session, chat_id, admin, user_id=None):
        self.say(session, chat_id, "گزینه موردنظر را انتخاب کنید.", self.menu(session, admin, user_id))

    def menu(self, session, admin=False, user_id=None):
        markup = reply(admin)
        markup["keyboard"] = [
            [button for button in row if enabled(session, TEXT_FEATURES.get(button["text"], ""))
             and (button["text"] != "👥 مدیریت ادمین‌ها" or user_id == self.config.admin_id)]
            for row in markup["keyboard"]
        ]
        markup["keyboard"] = [row for row in markup["keyboard"] if row]
        for row in markup["keyboard"]:
            for button in row:
                if button["text"] == "🎁 تخفیف‌های من":
                    button["text"] = records_button(session)
        if not markup["keyboard"]:
            return {"remove_keyboard": True}
        return markup

    def refresh_student_menus(self, session):
        students = session.scalars(select(Student).where(Student.active.is_(True), Student.chat_id.is_not(None)))
        admin_keys = set(session.scalars(select(Setting.key).where(Setting.key.startswith("admin:"))))
        markup = self.menu(session)
        for student in students:
            if student.bale_user_id != self.config.admin_id and f"admin:{student.bale_user_id}" not in admin_keys:
                self.say(session, student.chat_id, "منوی شما به‌روز شد.", markup)

    def admins_menu(self, session, chat_id, page=0):
        admins = list(session.scalars(select(Setting).where(Setting.key.startswith("admin:"))
                                      .order_by(Setting.key).offset(page * 15).limit(16)))
        rows = [[("افزودن ادمین", "admin_add")]]
        rows.extend([(f"حذف ادمین {item.key.split(':')[1]}", f"admin_remove:{item.key.split(':')[1]}")]
                    for item in admins[:15])
        navigation = []
        if page:
            navigation.append(("قبلی", f"admins:{page - 1}"))
        if len(admins) > 15:
            navigation.append(("بعدی", f"admins:{page + 1}"))
        if navigation:
            rows.append(navigation)
        self.say(session, chat_id,
                 f"مدیر اصلی: {self.config.admin_id}\nمدیر اصلی قابل حذف نیست. ادمین‌های دیگر به مدیریت بات دسترسی دارند، نه مدیریت ادمین‌ها.",
                 inline(rows))

    def allow(self, session, chat_id, features):
        if any(not enabled(session, feature) for feature in features):
            self.say(session, chat_id, "این امکان در حال حاضر توسط مدیر غیرفعال شده است.")
            return False
        return True

    def feature_menu(self, session, chat_id):
        self.say(session, chat_id, "برای فعال یا غیرفعال کردن هر امکان، دکمه آن را بزنید.", inline([
            [(
             f"{'✅ فعال' if enabled(session, key) else '⛔ غیرفعال'} | {records_title(session) if key == 'discounts' else title}",
             f"feature:{key}:{0 if enabled(session, key) else 1}")]
            for key, title in FEATURES.items()
        ]))

    def handle(self, session, update):
        callback = update.get("callback_query")
        if callback is not None and not isinstance(callback, dict):
            return
        message = callback.get("message", {}) if callback else update.get("message", {})
        if not isinstance(message, dict):
            return
        sender = callback.get("from", {}) if callback else message.get("from", {})
        chat = message.get("chat", {})
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            return
        user_id, chat_id = sender.get("id"), chat.get("id")
        if type(user_id) is not int or type(chat_id) is not int or chat.get("type") != "private":
            return
        admin = is_admin(session, user_id, self.config.admin_id)
        if user_id == self.config.admin_id:
            setting = session.get(Setting, "admin_chat_id")
            if setting:
                setting.value = str(chat_id)
            else:
                session.add(Setting(key="admin_chat_id", value=str(chat_id)))
        state = self.state(session, user_id)
        if state.data and (utcnow() - state.updated_at.replace(tzinfo=utcnow().tzinfo)) > timedelta(hours=2):
            self.set_state(state)
        if callback:
            callback_id = callback.get("id")
            if (isinstance(callback_id, str) and callback_id) or type(callback_id) is int:
                self.queue(session, "answerCallbackQuery", {"callback_query_id": str(callback_id)})
            data = callback.get("data")
            if not isinstance(data, str) or not data:
                return
            if data.startswith(("mr:", "my:")):
                self.registered_participant(session, chat_id, user_id, data)
                return
            # کال‌بک‌های مربوط به ثبت عدم حضور توسط دانش‌آموز دسترسی ادمین نمی‌خواهند
            if data.startswith("absence_cls:") or (data.startswith("confirm:") and state.data.get("mode") == "confirm_absence") or (data == "cancel" and not admin):
                self.student_callback(session, chat_id, user_id, state, data)
                return
            if not admin:
                self.say(session, chat_id, "دسترسی مجاز نیست.")
                return
            if data.startswith("mt:"):
                self.meeting_admin_callback(session, chat_id, user_id, state, data)
                return
            if data.startswith("coins:"):
                self.coins_callback(session, chat_id, user_id, state, data)
                return
            self.callback(session, chat_id, user_id, state, data)
            return
        raw_text = message.get("text")
        if not admin and not isinstance(raw_text, str):
            return
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        if text.split(maxsplit=1)[:1] == ["/start"]:
            text = "/start"
        if text == "/id":
            self.say(session, chat_id, f"شناسه عددی حساب شما: {user_id}")
            return
        if is_refresh(text):
            if not admin and session.scalar(select(Student.id).where(
                Student.bale_user_id == user_id, Student.chat_id == chat_id, Student.active.is_(True),
            )) is None:
                self.say(session, chat_id, "لطفاً کد ملی ۱۰ رقمی خود را ارسال کنید.", {"remove_keyboard": True})
                return
            self.say(session, chat_id, "✅ منوی شما به‌روز شد.", self.menu(session, admin, user_id))
            return
        feature = "discounts" if text == records_button(session) else TEXT_FEATURES.get(text)
        if feature and not self.allow(session, chat_id, [feature]):
            return
        if text in ("/start", "/cancel", "لغو", "منوی اصلی"):
            self.set_state(state)
            if admin:
                self.home(session, chat_id, True, user_id)
            else:
                student = session.scalar(
                    select(Student).where(Student.bale_user_id == user_id, Student.active.is_(True)))
                if student:
                    self.home(session, chat_id, False)
                else:
                    self.say(session, chat_id, "لطفاً کد ملی ۱۰ رقمی خود را ارسال کنید.", {"remove_keyboard": True})
            return
        if admin:
            self.admin_message(session, chat_id, user_id, state, message, text)
        else:
            self.student_message(session, chat_id, user_id, state, text)

    def student_callback(self, session, chat_id, user_id, state, data):
        parts = data.split(":")
        action = parts[0]
        if action == "cancel":
            self.set_state(state)
            self.home(session, chat_id, False)
            return
        if action == "absence_cls" and len(parts) == 2 and parts[1].isdigit():
            classroom_id = int(parts[1])
            classroom = session.get(Classroom, classroom_id)
            student_id = session.scalar(select(Student.id).where(
                Student.bale_user_id == user_id, Student.chat_id == chat_id, Student.active.is_(True),
            ))
            if not self.allow(session, chat_id, ["absence"]):
                return
            if not classroom or student_id is None or not session.get(Enrollment, (classroom_id, student_id)):
                self.say(session, chat_id, "کلاس یافت نشد.")
                return
            self.set_state(state, mode="absence_date", classroom_id=classroom.id, classroom_title=classroom.title)
            _, today_shamsi = excel.today()
            self.say(
                session, chat_id,
                f"کلاس انتخاب شده: {classroom.title}\n\n"
                f"لطفاً تاریخ عدم حضور را به صورت شمسی ارسال کنید (مثال: {today_shamsi})\n"
                f"همچنین می‌توانید کلمه «امروز» یا «فردا» را ارسال فرمایید."
            )
            return
        if action == "confirm":
            if len(parts) != 2 or parts[1] != state.data.get("nonce"):
                self.say(session, chat_id, "این درخواست منقضی شده یا قبلاً ثبت شده است.")
                return
            if state.data.get("mode") == "confirm_absence":
                if not self.allow(session, chat_id, ["absence"]):
                    return
                from datetime import date as dt_date
                absence_d = dt_date.fromisoformat(state.data["absence_date"])
                student = session.scalar(select(Student).where(
                    Student.bale_user_id == user_id, Student.chat_id == chat_id, Student.active.is_(True),
                ))
                if not student or not session.get(Enrollment, (state.data["classroom_id"], student.id)):
                    self.say(session, chat_id, "اطلاعات دانش‌آموز یافت نشد.")
                    self.set_state(state)
                    return
                register_absence(
                    session=session,
                    student_id=student.id,
                    classroom_id=state.data["classroom_id"],
                    absence_date=absence_d,
                    reason=state.data.get("reason"),
                )
                self.audit(session, user_id, "register_absence", classroom_id=state.data["classroom_id"], date=str(absence_d))
                self.set_state(state)
                self.say(
                    session, chat_id,
                    "✅ درخواست عدم حضور شما با موفقیت ثبت شد و به اطلاع مدیریت خواهد رسید.",
                    self.menu(session, False, user_id)
                )

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
            self.say(session, chat_id, f"{student.full_name} عزیز، خوش آمدید.", self.menu(session))
            return

        if text == COINS_STUDENT_BUTTON:
            if student.chat_id == chat_id:
                self.set_state(state)
                self.coins_balance(session, chat_id, student.id)
            return
        if text == "📅 جلسات من":
            self.set_state(state)
            if student.chat_id == chat_id:
                self.meeting_list(session, chat_id, student=student)
            return
        mode = state.data.get("mode")
        if mode == "absence_date":
            clean_date = excel.normalize(text).replace("-", "/").strip()
            target_gregorian = None
            if clean_date in ("امروز", "فردا"):
                today, _ = excel.today()
                target_gregorian = today + timedelta(days=1 if clean_date == "فردا" else 0)
                jalali_str = jdatetime.date.fromgregorian(date=target_gregorian).strftime("%Y/%m/%d")
            else:
                parts = clean_date.split("/")
                if len(parts) == 3 and all(p.isdigit() for p in parts):
                    try:
                        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
                        j_d = jdatetime.date(y, m, d)
                        target_gregorian = j_d.togregorian()
                        jalali_str = j_d.strftime("%Y/%m/%d")
                    except (ValueError, OverflowError):
                        pass
            if not target_gregorian:
                self.say(session, chat_id, "فرمت تاریخ نامعتبر است. لطفاً به صورت نمونه (مثلاً 1403/08/15) یا عبارت «امروز» یا «فردا» ارسال کنید.")
                return

            self.set_state(
                state,
                mode="absence_reason",
                classroom_id=state.data["classroom_id"],
                classroom_title=state.data["classroom_title"],
                absence_date=target_gregorian.isoformat(),
                jalali_date=jalali_str,
            )
            self.say(session, chat_id, "علت عدم حضور را بنویسید (در صورت تمایل به خالی ماندن، کاراکتر «-» را بفرستید):")
            return

        elif mode == "absence_reason":
            reason = None if text.strip() in ("-", "") else text.strip()[:1000]
            nonce = secrets.token_hex(8)
            self.set_state(
                state,
                mode="confirm_absence",
                nonce=nonce,
                classroom_id=state.data["classroom_id"],
                classroom_title=state.data["classroom_title"],
                absence_date=state.data["absence_date"],
                jalali_date=state.data["jalali_date"],
                reason=reason,
            )
            preview = (
                f"📋 پیش‌نمایش اطلاع‌رسانی عدم حضور:\n\n"
                f"🏫 کلاس: {state.data['classroom_title']}\n"
                f"📅 تاریخ: {state.data['jalali_date']}\n"
                f"📝 دلیل: {reason or 'ذکر نشده'}\n\n"
                f"آیا این درخواست ثبت و ارسال شود؟"
            )
            self.say(session, chat_id, preview, inline([[("تأیید و ارسال", f"confirm:{nonce}"), ("لغو", "cancel")]]))
            return

        if text == "🏫 کلاس‌های من":
            classes = session.execute(
                select(Classroom)
                    .join(Enrollment, Enrollment.classroom_id == Classroom.id)
                    .where(Enrollment.student_id == student.id)
                    .order_by(Classroom.title)
            ).scalars().all()
            if not classes:
                self.say(session, chat_id, "شما در حال حاضر در هیچ کلاسی ثبت‌نام نیستید.")
                return
            items = []
            for cls in classes:
                items.append(
                    f"🏫 عنوان کلاس: {cls.title}\n"
                    f"👨‍🏫 استاد: {cls.instructor or 'ثبت نشده'}\n"
                    f"⏰ زمان برگزاری: {cls.schedule or 'ثبت نشده'}\n"
                    f"📍 مکان/آدرس: {cls.location or 'ثبت نشده'}"
                )
            self.say(session, chat_id, "📋 لیست کلاس‌های شما:\n\n" + "\n\n────────────\n\n".join(items))
        elif text == "📝 ثبت عدم حضور":
            classes = session.execute(
                select(Classroom)
                    .join(Enrollment, Enrollment.classroom_id == Classroom.id)
                    .where(Enrollment.student_id == student.id)
                    .order_by(Classroom.title)
            ).scalars().all()
            if not classes:
                self.say(session, chat_id, "شما در هیچ کلاسی عضو نیستید.")
                return
            rows = [[(cls.title, f"absence_cls:{cls.id}")] for cls in classes]
            rows.append([("لغو", "cancel")])
            self.set_state(state)
            self.say(session, chat_id, "کلاسی را که قصد ثبت عدم حضور در آن دارید انتخاب کنید:", inline(rows))
        elif text == "📋 حضور و غیاب من":
            records = session.execute(select(Attendance, Classroom.title).join(
                Classroom, Classroom.id == Attendance.classroom_id,
            ).where(Attendance.student_id == student.id).order_by(
                Attendance.session_date.desc(), Attendance.id.desc(),
            ).limit(30)).all()
            output = "\n\n".join(f"کلاس: {title}\n{item.jalali_date}: {item.status}" for item, title in records)
            self.say(session, chat_id, output or "هنوز حضور و غیابی برای شما ثبت نشده است.")
        elif text in ("🎁 تخفیف‌های من", records_button(session)):
            discounts = session.execute(select(Discount, Classroom.title).join(
                Classroom, Classroom.id == Discount.classroom_id,
            ).join(Enrollment, (Enrollment.classroom_id == Discount.classroom_id) &
                   (Enrollment.student_id == Discount.student_id)).where(
                Discount.student_id == student.id,
            ).order_by(Discount.id)).all()
            title = records_title(session)
            value_label = "کد تخفیف" if title == "تخفیف‌ها" else "عنوان/کد"
            output = "\n\n".join(f"کلاس: {class_title}\n{value_label}: {item.code}\n{item.description}"
                                 for item, class_title in discounts)
            empty = "تخفیفی برای شما ثبت نشده است." if title == "تخفیف‌ها" else f"در بخش «{title}» رکوردی برای شما ثبت نشده است."
            self.say(session, chat_id, (f"{title}\n\n" + output) if output else empty)
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
        count = session.scalar(
            select(func.count()).select_from(Enrollment).where(Enrollment.classroom_id == classroom.id))
        identifier = classroom.id
        title = records_title(session)
        rows = [
            [("ویرایش نام کلاس", f"rename:{identifier}")],
            [("👨‍🏫 ویرایش استاد", f"edit_instructor:{identifier}"), ("⏰ ویرایش زمان", f"edit_schedule:{identifier}")],
            [("📍 ویرایش مکان/آدرس", f"edit_location:{identifier}")],
            [("نمونه کاربران", f"template:students:{identifier}"), ("لیست فعلی", f"export:students:{identifier}")],
            [("بارگذاری کاربران", f"upload:students:{identifier}")],
            [("اکسل حضور و غیاب امروز", f"export:attendance:{identifier}")],
            [("بارگذاری حضور و غیاب", f"upload:attendance:{identifier}")],
            [("ثبت حضور و غیاب داخل بات", f"attend:{identifier}")],
            [(f"اکسل {title}", f"export:discounts:{identifier}")],
            [(f"بارگذاری {title}", f"upload:discounts:{identifier}")],
            [(f"ثبت رکورد در {title}", f"record:{identifier}")],
            [("🪙 ثبت سکه", f"coins:categories:{identifier}:0"),
             ("📥 اکسل سکه‌ها", f"coins:report:{identifier}")],
            [("ارسال پیام به کلاس", f"broadcast:{identifier}")],
            [("حذف عضویت دانش‌آموز", f"remove:{identifier}")],
        ]
        rows = [[(btn_title, data) for btn_title, data in row
                 if all(enabled(session, feature) for feature in callback_features(data))] for row in rows]

        info_text = (
            f"🏫 کلاس: {classroom.title}\n"
            f"👨‍🏫 استاد: {classroom.instructor or 'ثبت نشده'}\n"
            f"⏰ زمان: {classroom.schedule or 'ثبت نشده'}\n"
            f"📍 مکان: {classroom.location or 'ثبت نشده'}\n"
            f"👥 تعداد اعضا: {count}"
        )
        self.say(session, chat_id, info_text, inline([row for row in rows if row]))

    def admin_message(self, session, chat_id, user_id, state, message, text):
        if text == COINS_ADMIN_BUTTON:
            self.coins_menu(session, chat_id, state)
            return
        if text == "📅 جلسات":
            self.set_state(state)
            self.meeting_list(session, chat_id)
            return
        if state.data.get("mode", "").startswith("meeting_"):
            self.meeting_admin_message(session, chat_id, state, message, text)
            return
        if text == "👥 مدیریت ادمین‌ها":
            if user_id != self.config.admin_id:
                self.say(session, chat_id, "فقط مدیر اصلی اجازه مدیریت ادمین‌ها را دارد.")
                return
            self.set_state(state)
            self.admins_menu(session, chat_id)
            return
        if text == "📋 گزارش درخواست‌های عدم حضور":
            self.set_state(state)
            requests = get_pending_absences(session)
            if not requests:
                self.say(session, chat_id, "در حال حاضر هیچ درخواست عدم حضور بررسی‌نشده‌ای وجود ندارد.")
                return
            content = excel.export_absences_excel(requests)
            current_date, jalali = excel.today()
            self.queue(session, "sendDocument", {
                "chat_id": chat_id,
                "caption": f"📋 گزارش درخواست‌های عدم حضور دانش‌آموزان\n📅 تاریخ گزارش: {jalali}\nتعداد رکوردهای ثبت‌شده: {len(requests)}",
                "_file": {
                    "name": f"absence_requests_{current_date.isoformat()}.xlsx",
                    "content": base64.b64encode(content).decode("ascii"),
                },
            })
            return
        if text == "🎁 نام بخش تخفیف/رکوردها":
            self.set_state(state, mode="records_title")
            self.say(session, chat_id,
                     f"نام فعلی: {records_title(session)}\nنام جدید بخش را در یک خط (حداکثر ۶۰ کاراکتر) بفرستید. رکوردهای قبلی حفظ می‌شوند.")
            return
        if text == "⚙️ امکانات بات":
            self.set_state(state)
            self.feature_menu(session, chat_id)
            return
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
            self.say(session, chat_id, "وضعیت همه پیام‌ها:\n" + "\n".join(
                f"{labels.get(status, status)}: {count}" for status, count in counts),
                     inline([[("تلاش مجدد ناموفق‌ها", "retry_failed")]]))
            return
        mode = state.data.get("mode")
        if not self.allow(session, chat_id, state_features(state.data)):
            self.set_state(state)
            return
        if mode and mode.startswith("coins_"):
            self.coins_admin_message(session, chat_id, user_id, state, text)
            return
        if mode == "admin_add":
            if user_id != self.config.admin_id:
                self.set_state(state)
                self.say(session, chat_id, "فقط مدیر اصلی اجازه مدیریت ادمین‌ها را دارد.")
                return
            identifier = excel.normalize(text)
            if (not identifier.isascii() or not identifier.isdigit()
                    or not 0 < int(identifier) <= 9223372036854775807):
                self.say(session, chat_id, "شناسه عددی مثبت حساب بله را بفرستید، نه شماره تلفن یا نام کاربری.")
                return
            identifier = int(identifier)
            if is_admin(session, identifier, self.config.admin_id):
                self.say(session, chat_id, "این حساب از قبل ادمین است.")
                return
            nonce = secrets.token_hex(8)
            self.set_state(state, mode="confirm_admin", operation="add", admin_id=identifier, nonce=nonce)
            self.say(session, chat_id, f"حساب {identifier} به‌عنوان ادمین با دسترسی مدیریت بات اضافه شود؟",
                     inline([[("تأیید افزودن", f"confirm:{nonce}"), ("لغو", "cancel")]]))
        elif mode == "records_title":
            if not text or len(text) > 60 or any(ord(character) < 32 for character in text):
                self.say(session, chat_id, "نام بخش باید یک خط و بین ۱ تا ۶۰ کاراکتر باشد.")
                return
            save_setting(session, "discounts_title", text)
            self.audit(session, user_id, "rename_records_section", title=text)
            self.set_state(state)
            session.flush()
            self.refresh_student_menus(session)
            self.say(session, chat_id, "نام بخش به‌روز شد؛ رکوردهای قبلی حفظ شدند.", self.menu(session, True, user_id))
        elif mode == "record_student":
            try:
                code = excel.national_id(text)
            except ValueError as error:
                self.say(session, chat_id, str(error))
                return
            student = session.scalar(select(Student).join(Enrollment).where(
                Enrollment.classroom_id == state.data["classroom_id"], Student.national_id == code,
                Student.active.is_(True),
            ))
            if student is None:
                self.say(session, chat_id, "دانش‌آموز عضو فعال این کلاس نیست.")
                return
            self.set_state(state, mode="record_code", classroom_id=state.data["classroom_id"],
                           national_id=student.national_id, name=student.full_name)
            self.say(session, chat_id,
                     f"دانش‌آموز: {student.full_name}\nعنوان یا کد یکتای رکورد را بفرستید (حداکثر ۲۰۰ کاراکتر)؛ مثلاً کد تخفیف، نمره ریاضی یا لینک دوره.")
        elif mode == "record_code":
            if not text or len(text) > 200:
                self.say(session, chat_id, "عنوان/کد رکورد باید بین ۱ تا ۲۰۰ کاراکتر باشد.")
                return
            self.set_state(state, **{**state.data, "mode": "record_description", "code": text})
            self.say(session, chat_id,
                     "مقدار، متن یا توضیحات رکورد را بفرستید (حداکثر ۲۰۰۰ کاراکتر). برای مقدار خالی «-» بفرستید.")
        elif mode == "record_description":
            if not text or len(text) > 2000:
                self.say(session, chat_id,
                         "مقدار/توضیحات باید بین ۱ تا ۲۰۰۰ کاراکتر باشد؛ برای خالی ماندن «-» بفرستید.")
                return
            description = "" if text == "-" else text
            pending = {"kind": "discounts", "classroom_id": state.data["classroom_id"], "rows": [{
                "national_id": state.data["national_id"], "name": state.data["name"],
                "code": state.data["code"], "description": description,
            }]}
            nonce = secrets.token_hex(8)
            self.set_state(state, mode="confirm_import", pending=pending, nonce=nonce, source="discounts")
            row = pending["rows"][0]
            self.say(session, chat_id,
                     f"بخش: {records_title(session)}\nدانش‌آموز: {row['name']}\nعنوان/کد: {row['code']}\n{description}\nثبت شود؟ عنوان/کد تکراری برای همین دانش‌آموز در همین کلاس به‌روزرسانی می‌شود؛ عنوان جدید رکورد جدا می‌سازد.",
                     inline([[("تأیید ثبت", f"confirm:{nonce}"), ("لغو", "cancel")]]))
        elif mode in ("create", "rename"):
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
        elif mode in ("edit_instructor", "edit_schedule", "edit_location"):
            classroom = session.get(Classroom, state.data["classroom_id"])
            if not classroom:
                self.say(session, chat_id, "کلاس یافت نشد.")
                self.set_state(state)
                return
            val = None if text == "-" else text[:200]
            if mode == "edit_instructor":
                classroom.instructor = val
                action_name = "ویرایش استاد کلاس"
            elif mode == "edit_schedule":
                classroom.schedule = val
                action_name = "ویرایش زمان کلاس"
            else:
                classroom.location = val
                action_name = "ویرایش مکان کلاس"
            self.audit(session, user_id, mode, classroom_id=classroom.id, value=val)
            self.set_state(state)
            self.say(session, chat_id, f"{action_name} با موفقیت انجام شد.")
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
                pending = excel.parse(content, state.data["kind"], state.data["classroom_id"],
                                      self.config.max_excel_rows)
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
            self.set_state(state, mode="confirm_import", nonce=nonce, pending=pending, source="excel")
            preview = f"تعداد ردیف معتبر: {len(pending['rows'])}\n"
            if pending["kind"] == "attendance":
                jalali = jdatetime.date.fromgregorian(date=date.fromisoformat(pending["session_date"])).strftime("%Y/%m/%d")
                present = sum(row["status"] == "حاضر" for row in pending["rows"])
                preview += f"تاریخ جلسه: {jalali}\nحاضر: {present} | غایب: {len(pending['rows']) - present}\n"
            preview += "ثبت نهایی انجام شود؟ رکوردهای موجود به‌روز می‌شوند و اعضای حذف‌شده از فایل، حذف نخواهند شد."
            self.say(session, chat_id, preview, inline([[("تأیید ثبت", f"confirm:{nonce}"), ("لغو", "cancel")]]))
        elif mode == "broadcast":
            if not any(key in message for key in
                       ("text", "photo", "document", "video", "audio", "voice", "animation", "sticker")):
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
                self.set_state(state, mode="confirm_remove", nonce=nonce, classroom_id=enrollment.classroom_id,
                               student_id=student.id)
                self.say(session, chat_id, f"عضویت {student.full_name} از کلاس حذف شود؟ سوابق حفظ می‌شوند.",
                         inline([[("حذف عضویت", f"confirm:{nonce}"), ("لغو", "cancel")]]))
            else:
                self.set_state(state)
                self.say(session, chat_id,
                         f"{student.full_name}\nکد ملی: {student.national_id}\nشماره: {student.phone}\nفعال: {'بله' if student.active else 'خیر'}\nمتصل: {'بله' if student.chat_id else 'خیر'}",
                         inline([
                             [("غیرفعال‌سازی" if student.active else "فعال‌سازی", f"toggle:{student.id}")],
                             [("قطع اتصال حساب بله", f"unlink:{student.id}")],
                         ]))
        else:
            self.say(session, chat_id, "از منو گزینه‌ای انتخاب کنید یا «لغو» بفرستید.",
                     self.menu(session, True, user_id))

    def recipients(self, session, classroom_id):
        statement = select(Student.chat_id).where(Student.active.is_(True), Student.chat_id.is_not(None))
        if classroom_id is not None:
            statement = statement.join(Enrollment).where(Enrollment.classroom_id == classroom_id)
        return list(session.scalars(statement.distinct()))

    def callback(self, session, chat_id, user_id, state, data):
        parts = data.split(":")
        action = parts[0]
        if action in ("admins", "admin_add", "admin_remove") or (
                action == "confirm" and state.data.get("mode") == "confirm_admin"):
            if user_id != self.config.admin_id:
                self.say(session, chat_id, "فقط مدیر اصلی اجازه مدیریت ادمین‌ها را دارد.")
                return
        if action == "admins" and len(parts) == 2 and parts[1].isascii() and parts[1].isdigit():
            self.set_state(state)
            self.admins_menu(session, chat_id, int(parts[1]))
            return
        if action == "admin_add":
            self.set_state(state, mode="admin_add")
            self.say(session, chat_id,
                     "شناسه عددی حساب بله ادمین جدید را بفرستید (نه شماره تلفن یا نام کاربری). کاربر می‌تواند با ارسال /id به بات، شناسه خود را ببیند.")
            return
        if action == "admin_remove" and len(parts) == 2 and parts[1].isascii() and parts[1].isdigit():
            identifier = int(parts[1])
            if identifier == self.config.admin_id:
                self.say(session, chat_id, "مدیر اصلی قابل حذف نیست.")
                return
            if not is_admin(session, identifier, self.config.admin_id):
                self.say(session, chat_id, "این حساب ادمین نیست.")
                return
            nonce = secrets.token_hex(8)
            self.set_state(state, mode="confirm_admin", operation="remove", admin_id=identifier, nonce=nonce)
            self.say(session, chat_id, f"دسترسی ادمین حساب {identifier} حذف شود؟",
                     inline([[("تأیید حذف", f"confirm:{nonce}"), ("لغو", "cancel")]]))
            return
        if action == "feature":
            if len(parts) != 3 or parts[1] not in FEATURES or parts[2] not in ("0", "1"):
                return
            key = "feature:" + parts[1]
            setting = session.get(Setting, key)
            if setting is None:
                session.add(Setting(key=key, value=parts[2]))
            else:
                setting.value = parts[2]
            self.audit(session, user_id, "update_feature", feature=parts[1], enabled=parts[2] == "1")
            self.set_state(state)
            session.flush()
            if parts[1] in ("attendance", "discounts", "contacts", "coins"):
                self.refresh_student_menus(session)
            self.feature_menu(session, chat_id)
            self.home(session, chat_id, True, user_id)
            return
        required = callback_features(data)
        if action in ("confirm", "mark"):
            required += state_features(state.data)
        if not self.allow(session, chat_id, required):
            self.set_state(state)
            return
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
            self.home(session, chat_id, True, user_id)
            return
        if action == "confirm":
            if len(parts) != 2 or parts[1] != state.data.get("nonce"):
                self.say(session, chat_id, "این درخواست منقضی شده یا قبلاً انجام شده است.")
                return
            mode = state.data.get("mode")
            if mode == "confirm_admin":
                identifier = state.data["admin_id"]
                if identifier == self.config.admin_id:
                    self.say(session, chat_id, "مدیر اصلی قابل تغییر نیست.")
                    self.set_state(state)
                    return
                operation = state.data["operation"]
                if operation == "add":
                    save_setting(session, f"admin:{identifier}", "1")
                else:
                    setting = session.get(Setting, f"admin:{identifier}")
                    if setting:
                        session.delete(setting)
                target_state = session.get(Conversation, identifier)
                if target_state:
                    self.set_state(target_state)
                self.audit(session, user_id, operation + "_admin", admin_id=identifier)
                self.say(session, chat_id,
                         "ادمین اضافه شد؛ حساب جدید باید /start بفرستد." if operation == "add" else "دسترسی ادمین حذف شد.")
                if operation == "remove":
                    self.say(session, identifier, "دسترسی ادمین شما حذف شد. برای دریافت منوی خود /start بفرستید.",
                             {"remove_keyboard": True})
            elif mode == "confirm_import":
                try:
                    count = apply_import(session, state.data["pending"])
                except ValueError as error:
                    self.say(session, chat_id, str(error))
                    self.set_state(state)
                    return
                self.audit(session, user_id, "import_" + state.data["pending"]["kind"],
                           classroom_id=state.data["pending"]["classroom_id"], rows=count)
                suffix = "؛ اعلان‌ها در صف ارسال قرار گرفتند." if state.data["pending"]["kind"] == "attendance" else "."
                self.say(session, chat_id, f"ثبت انجام شد. تعداد رکورد جدید یا به‌روزشده: {count}{suffix}")
            elif mode == "confirm_broadcast":
                targets = self.recipients(session, state.data["classroom_id"])
                for target in targets:
                    session.add(Outbox(method="copyMessage", payload={
                        "chat_id": target, "from_chat_id": state.data["source_chat_id"],
                        "message_id": state.data["source_message_id"],
                    }, dedupe_key=f"broadcast:{parts[1]}:{target}"))
                self.audit(session, user_id, "broadcast", classroom_id=state.data["classroom_id"],
                           recipients=len(targets))
                self.say(session, chat_id,
                         f"پیام برای {len(targets)} نفر در صف قرار گرفت. نتیجه از «وضعیت ارسال‌ها» قابل مشاهده است.")
            elif mode == "confirm_remove":
                enrollment = session.get(Enrollment, (state.data["classroom_id"], state.data["student_id"]))
                if enrollment:
                    session.delete(enrollment)
                self.audit(session, user_id, "remove_enrollment", classroom_id=state.data["classroom_id"],
                           student_id=state.data["student_id"])
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
                failed = list(session.scalars(
                    select(Outbox).where(Outbox.status == "failed", Outbox.method != "answerCallbackQuery")))
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
            elif action in ("edit_instructor", "edit_schedule", "edit_location"):
                self.set_state(state, mode=action, classroom_id=identifier)
                prompts = {
                    "edit_instructor": "نام استاد کلاس را بفرستید (برای خالی کردن «-» بفرستید):",
                    "edit_schedule": "زمان و ساعت برگزاری کلاس را بفرستید (مثال: دوشنبه‌ها ساعت ۱۶ تا ۱۸ / برای خالی کردن «-» بفرستید):",
                    "edit_location": "مکان یا لینک برگزاری کلاس را بفرستید (برای خالی کردن «-» بفرستید):",
                }
                self.say(session, chat_id, prompts[action])
            elif action in ("rename", "remove", "broadcast", "record"):
                self.set_state(state, mode="record_student" if action == "record" else action, classroom_id=identifier)
                prompts = {"rename": "نام جدید کلاس را بفرستید.", "remove": "کد ملی دانش‌آموز را بفرستید.",
                           "broadcast": "پیام متنی یا رسانه‌ای موردنظر برای این کلاس را بفرستید.",
                           "record": f"برای ثبت رکورد در «{records_title(session)}»، کد ملی دانش‌آموز عضو کلاس را بفرستید."}
                self.say(session, chat_id, prompts[action])
            return
        if len(parts) == 3 and parts[1] in excel.HEADERS and parts[2].isdigit():
            kind, classroom_id = parts[1], int(parts[2])
            if session.get(Classroom, classroom_id) is None:
                self.say(session, chat_id, "کلاس یافت نشد.")
                return
            if action == "upload":
                self.set_state(state, mode="upload", kind=kind, classroom_id=classroom_id)
                self.say(session, chat_id,
                         "فایل xlsx تکمیل‌شده همین کلاس را ارسال کنید؛ سپس خلاصه و تأیید نمایش داده می‌شود.")
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
        self.set_state(state, mode="confirm_import", pending=pending, nonce=nonce, source="manual_attendance")
        self.say(session, chat_id,
                 f"حاضر: {present} | غایب: {len(roster) - present}\nثبت و ارسال اعلان داخل بات تأیید شود؟",
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
                    rows.extend(
                        (student.national_id, student.full_name, item.code, item.description) for item in discounts)
                    if not discounts:
                        rows.append((student.national_id, student.full_name, "", ""))
        content = excel.workbook(kind, rows, classroom_id, current_date.isoformat() if kind == "attendance" else None)
        self.queue(session, "sendDocument", {
            "chat_id": chat_id,
            "caption": (f"بخش: {records_title(session)}\n" if kind == "discounts" else "")
                       + f"کلاس {classroom_id} | {jalali}\nعنوان ستون‌ها و شیت مخفی را تغییر ندهید. کد ملی و شماره تماس را به صورت Text نگه دارید.",
            "_file": {"name": f"{kind}-{classroom_id}-{current_date.isoformat()}.xlsx",
                      "content": base64.b64encode(content).decode("ascii")},
        })
