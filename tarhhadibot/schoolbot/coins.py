import base64
import re
import secrets
from datetime import timezone
from io import BytesIO
from zoneinfo import ZoneInfo

import jdatetime
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from sqlalchemy import and_, func, or_, select

from . import excel
from .features import callback_features, enabled
from .models import MAX_COINS_PER_AWARD, Classroom, CoinAward, CoinCategory, Enrollment, Outbox, Student


PAGE_SIZE = 10
TEHRAN = ZoneInfo("Asia/Tehran")
TITLE_TRANSLATION = str.maketrans("يك", "یک")


def _keyboard(rows):
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


def _jalali(value):
    local = (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(TEHRAN)
    return f"{jdatetime.date.fromgregorian(date=local.date()):%Y/%m/%d} {local:%H:%M}"


def _sheet(book, title, headers):
    sheet = book.create_sheet(title)
    sheet.sheet_view.rightToLeft = True
    sheet.freeze_panes = "A2"
    cells = []
    for index, header in enumerate(headers, 1):
        sheet.column_dimensions[get_column_letter(index)].width = 26
        cell = WriteOnlyCell(sheet, value=header)
        cell.font = Font(bold=True)
        cells.append(cell)
    sheet.append(cells)
    return sheet


def _append(sheet, values):
    cells = []
    for value in values:
        if isinstance(value, str):
            cell = WriteOnlyCell(sheet, value=ILLEGAL_CHARACTERS_RE.sub("", value))
            cell.data_type = "s"
            cell.number_format = "@"
            cells.append(cell)
        else:
            cells.append(value)
    sheet.append(cells)


def coin_report(session, classroom_id):
    book = Workbook(write_only=True)
    summary = _sheet(book, "خلاصه سکه‌ها", ["کد ملی", "نام و نام خانوادگی", "وضعیت عضویت", "سکه در این کلاس"])
    history = _sheet(book, "ریز امتیازات", ["کد ملی", "نام و نام خانوادگی", "بخش امتیاز", "سکه", "زمان ثبت (شمسی)"])
    totals = select(CoinAward.student_id, func.sum(CoinAward.amount).label("total")).where(
        CoinAward.classroom_id == classroom_id,
    ).group_by(CoinAward.student_id).subquery()
    roster = select(Student.national_id, Student.full_name, Student.active,
                    Enrollment.student_id, totals.c.total).outerjoin(totals, totals.c.student_id == Student.id).outerjoin(
        Enrollment, and_(Enrollment.student_id == Student.id, Enrollment.classroom_id == classroom_id),
    ).where(or_(Enrollment.student_id.is_not(None), totals.c.student_id.is_not(None))).order_by(
        Student.full_name, Student.id,
    )
    for national_id, name, active, member_id, total in session.execute(roster):
        status = "عضو سابق" if member_id is None else "فعال" if active else "غیرفعال"
        _append(summary, (national_id, name, status, total or 0))
    awards = select(Student.national_id, Student.full_name, CoinAward.category_title,
                    CoinAward.amount, CoinAward.created_at).select_from(CoinAward).join(Student).where(
        CoinAward.classroom_id == classroom_id,
    ).order_by(CoinAward.id.desc()).execution_options(yield_per=1000)
    for national_id, name, category, amount, created_at in session.execute(awards):
        _append(history, (national_id, name, category, amount, _jalali(created_at)))
    result = BytesIO()
    book.save(result)
    return result.getvalue()


def valid_coin_delivery(session, message):
    _, award_id, recipient_id = message.dedupe_key.split(":")
    award = session.get(CoinAward, int(award_id))
    student = session.get(Student, award.student_id) if award else None
    return (student is not None and student.active and student.bale_user_id == int(recipient_id)
            and student.chat_id == message.payload.get("chat_id"))


class CoinsMixin:
    def coins_menu(self, session, chat_id, state):
        self.set_state(state)
        self.say(session, chat_id, "🪙 مدیریت امتیازات و سکه‌های هادی", _keyboard([
            [("➕ ایجاد بخش امتیاز", "coins:new"), ("⚙️ ویرایش ارزش بخش‌ها", "coins:manage:0")],
            [("🏫 ثبت امتیاز و گزارش کلاس‌ها", "coins:classes:0")],
        ]))

    def coins_balance(self, session, chat_id, student_id):
        total = session.scalar(select(func.sum(CoinAward.amount)).where(CoinAward.student_id == student_id)) or 0
        recent = session.execute(select(CoinAward, Classroom.title).join(Classroom).where(
            CoinAward.student_id == student_id,
        ).order_by(CoinAward.id.desc()).limit(10)).all()
        lines = [f"🪙 مجموع سکه‌های هادی شما: {total:,}"]
        if recent:
            lines.append("\nآخرین امتیازها:")
            lines.extend(f"+{award.amount:,} | {award.category_title} | {title} | {_jalali(award.created_at)}"
                         for award, title in recent)
        else:
            lines.append("هنوز امتیازی برای شما ثبت نشده است.")
        self.say(session, chat_id, "\n".join(lines))

    def _coins_page(self, session, chat_id, query, page, prefix, heading, empty, button, back):
        items = list(session.scalars(query.offset(page * PAGE_SIZE).limit(PAGE_SIZE + 1)))
        rows = [[button(item)] for item in items[:PAGE_SIZE]]
        navigation = []
        if page:
            navigation.append(("قبلی", f"{prefix}:{page - 1}"))
        if len(items) > PAGE_SIZE:
            navigation.append(("بعدی", f"{prefix}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([("بازگشت", back)])
        self.say(session, chat_id, heading if items else empty, _keyboard(rows))

    def coins_admin_message(self, session, chat_id, user_id, state, text):
        mode = state.data["mode"]
        if mode == "coins_title":
            title = " ".join(text.translate(TITLE_TRANSLATION).split())
            if not 1 <= len(title) <= 80 or any(
                not char.isprintable() and char != "\u200c" for char in title
            ):
                self.say(session, chat_id, "نام بخش باید بین ۱ تا ۸۰ نویسهٔ قابل نمایش باشد.")
                return
            if session.scalar(select(CoinCategory.id).where(CoinCategory.title == title)) is not None:
                self.say(session, chat_id, "این نام قبلاً ثبت شده است. نام دیگری بفرستید.")
                return
            self.set_state(state, mode="coins_amount", title=title)
            self.say(session, chat_id, f"برای «{title}» تعداد سکهٔ هر بار ثبت را بفرستید.")
        elif mode == "coins_amount":
            raw = excel.normalize(text)
            if not re.fullmatch(r"[0-9]{1,6}", raw) or not 1 <= int(raw) <= MAX_COINS_PER_AWARD:
                self.say(session, chat_id, f"یک عدد صحیح از ۱ تا {MAX_COINS_PER_AWARD:,} بفرستید.")
                return
            amount = int(raw)
            category_id = state.data.get("category_id")
            if category_id is None:
                title = state.data["title"]
                if session.scalar(select(CoinCategory.id).where(CoinCategory.title == title)) is not None:
                    self.set_state(state, mode="coins_title")
                    self.say(session, chat_id, "این نام قبلاً ثبت شده است. نام دیگری بفرستید.")
                    return
                category = CoinCategory(title=title, amount=amount)
                session.add(category)
                session.flush()
                self.audit(session, user_id, "coin_category_create", category_id=category.id, amount=amount)
            else:
                category = session.get(CoinCategory, category_id)
                if category is None:
                    self.set_state(state)
                    self.say(session, chat_id, "این بخش پیدا نشد.")
                    return
                self.audit(session, user_id, "coin_category_update", category_id=category.id,
                           previous_amount=category.amount, amount=amount)
                category.amount = amount
            self.set_state(state)
            self.say(session, chat_id, f"✅ «{category.title}»: {amount:,} سکه برای هر ثبت.",
                     _keyboard([[("مدیریت امتیازات", "coins:menu")]]))
        else:
            self.say(session, chat_id, "برای ثبت امتیاز از دکمهٔ تأیید یا لغو استفاده کنید.")

    def coins_callback(self, session, chat_id, user_id, state, data):
        if not self.allow(session, chat_id, callback_features(data)):
            if state.data.get("mode", "").startswith("coins_"):
                self.set_state(state)
            return
        _, action, *args = data.split(":")
        if action == "confirm":
            if len(args) == 1:
                self._coins_confirm(session, chat_id, user_id, state, args[0])
            return
        if action == "menu" and not args:
            self.coins_menu(session, chat_id, state)
            return
        if action == "new" and not args:
            self.set_state(state, mode="coins_title")
            self.say(session, chat_id, "نام بخش امتیاز را بفرستید؛ مثلاً فعالیت کلاسی.")
            return
        lengths = {"manage": 1, "classes": 1, "edit": 1, "class": 1, "categories": 2,
                   "students": 3, "preview": 3, "report": 1}
        if lengths.get(action) != len(args) or not all(re.fullmatch(r"[0-9]{1,10}", arg) for arg in args):
            return
        ids = tuple(map(int, args))
        if any(value > 2147483647 for value in ids):
            return
        self.set_state(state)
        if action == "manage":
            self._coins_page(session, chat_id, select(CoinCategory).order_by(CoinCategory.id), ids[0], "coins:manage",
                             "بخش موردنظر را برای ویرایش انتخاب کنید.", "بخشی برای نمایش وجود ندارد.",
                             lambda item: (f"{item.title} | {item.amount:,} سکه", f"coins:edit:{item.id}"), "coins:menu")
            return
        if action == "edit":
            category = session.get(CoinCategory, ids[0])
            if category:
                self.set_state(state, mode="coins_amount", category_id=category.id)
                self.say(session, chat_id, f"«{category.title}» اکنون {category.amount:,} سکه دارد. "
                         "تعداد جدید را بفرستید؛ ثبت‌های قبلی تغییر نمی‌کنند.")
            else:
                self.say(session, chat_id, "این بخش پیدا نشد.")
            return
        if action == "classes":
            self._coins_page(session, chat_id, select(Classroom).order_by(Classroom.id), ids[0], "coins:classes",
                             "کلاس را انتخاب کنید.", "کلاسی برای نمایش وجود ندارد.",
                             lambda item: (item.title, f"coins:class:{item.id}"), "coins:menu")
            return
        classroom = session.get(Classroom, ids[0])
        if classroom is None:
            self.say(session, chat_id, "این کلاس پیدا نشد.")
            return
        if action == "class":
            rows = [[("🪙 ثبت امتیاز", f"coins:categories:{classroom.id}:0")]]
            if enabled(session, "excel"):
                rows.append([("📥 اکسل سکه‌های کلاس", f"coins:report:{classroom.id}")])
            rows.append([("بازگشت به کلاس‌ها", "coins:classes:0")])
            self.say(session, chat_id, f"امتیازات کلاس «{classroom.title}»", _keyboard(rows))
        elif action == "report":
            date, jalali = excel.today()
            self.queue(session, "sendDocument", {
                "chat_id": chat_id, "caption": f"📊 گزارش سکه‌ها | {classroom.title} | {jalali}",
                "_file": {"name": f"coins-{classroom.id}-{date.isoformat()}.xlsx",
                          "content": base64.b64encode(coin_report(session, classroom.id)).decode("ascii")},
            })
        elif action == "categories":
            self._coins_page(session, chat_id, select(CoinCategory).order_by(CoinCategory.id), ids[1],
                             f"coins:categories:{classroom.id}", f"بخش امتیاز برای «{classroom.title}» را انتخاب کنید.",
                             "بخشی برای نمایش وجود ندارد. در منوی امتیازات ابتدا یک بخش بسازید.",
                             lambda item: (f"{item.title} | {item.amount:,} سکه", f"coins:students:{classroom.id}:{item.id}:0"),
                             f"coins:class:{classroom.id}")
        else:
            category = session.get(CoinCategory, ids[1])
            if category is None:
                self.say(session, chat_id, "این بخش پیدا نشد.")
                return
            roster = select(Student).join(Enrollment).where(
                Enrollment.classroom_id == classroom.id, Student.active.is_(True),
            )
            if action == "students":
                self._coins_page(session, chat_id, roster.order_by(Student.full_name, Student.id), ids[2],
                                 f"coins:students:{classroom.id}:{category.id}", "دانش‌آموز را انتخاب کنید.",
                                 "دانش‌آموز فعالی برای نمایش وجود ندارد.",
                                 lambda item: (f"{item.full_name} | {item.national_id}",
                                               f"coins:preview:{classroom.id}:{category.id}:{item.id}"),
                                 f"coins:categories:{classroom.id}:0")
            elif action == "preview":
                student = session.scalar(roster.where(Student.id == ids[2]))
                if student is None:
                    self.say(session, chat_id, "این دانش‌آموز عضو فعال این کلاس نیست.")
                    return
                nonce = secrets.token_hex(8)
                self.set_state(state, mode="coins_confirm", nonce=nonce, classroom_id=classroom.id,
                               category_id=category.id, student_id=student.id, amount=category.amount)
                self.say(session, chat_id, f"ثبت {category.amount:,} سکه برای «{student.full_name}» تأیید شود؟"
                         f"\nکد ملی: {student.national_id}\nکلاس: {classroom.title}\nبخش: {category.title}",
                         _keyboard([[("✅ تأیید ثبت", f"coins:confirm:{nonce}"), ("لغو", "cancel")]]))

    def _coins_confirm(self, session, chat_id, user_id, state, nonce):
        pending = state.data
        if pending.get("mode") != "coins_confirm" or pending.get("nonce") != nonce:
            self.say(session, chat_id, "درخواست معتبر نیست یا قبلاً پردازش شده است.")
            return
        self.set_state(state)
        classroom = session.get(Classroom, pending["classroom_id"])
        category = session.get(CoinCategory, pending["category_id"])
        student = session.get(Student, pending["student_id"])
        if (classroom is None or student is None or not student.active
                or session.get(Enrollment, (classroom.id, student.id)) is None):
            self.say(session, chat_id, "ثبت انجام نشد؛ دانش‌آموز دیگر عضو فعال این کلاس نیست.")
            return
        if category is None or category.amount != pending["amount"]:
            self.say(session, chat_id, "ارزش بخش تغییر کرده است. ثبت امتیاز را دوباره شروع کنید.")
            return
        award = CoinAward(classroom_id=classroom.id, student_id=student.id, category_id=category.id,
                          category_title=category.title, amount=category.amount, actor_id=user_id)
        session.add(award)
        session.flush()
        linked = student.chat_id is not None and student.bale_user_id is not None
        if linked:
            session.add(Outbox(method="sendMessage", dedupe_key=f"coins:{award.id}:{student.bale_user_id}", payload={
                "chat_id": student.chat_id,
                "text": f"🪙 برای شما {award.amount:,} سکهٔ هادی ثبت شد.\n"
                        f"بخش: {award.category_title}\nکلاس: {classroom.title}\n"
                        "موجودی را در «سکه‌های هادی» ببینید.",
            }))
        self.say(session, chat_id, f"✅ {award.amount:,} سکه برای «{student.full_name}» ثبت شد.\n"
                 + ("اعلان در صف ارسال قرار گرفت." if linked else "حساب دانش‌آموز متصل نیست؛ اعلان ارسال نمی‌شود."),
                 _keyboard([[("ثبت امتیاز دیگر در این کلاس", f"coins:categories:{classroom.id}:0")]]))
