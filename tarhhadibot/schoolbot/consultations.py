import base64
import re
import secrets
from datetime import datetime, timedelta
from io import BytesIO

import jdatetime
from openpyxl import Workbook
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from . import excel
from .coins import _append
from .consultation_models import Consultation, ConsultationBooking, ConsultationFamilyClaim, ConsultationSlot
from .education_access import ID_MAX, bound_student, class_access, identifiers
from .education_models import GuardianAccount, GuardianLink
from .features import CONSULTATION_BUTTON, enabled
from .meetings import DAYS, TEHRAN, keyboard, parse_start, utc
from .models import Classroom, Enrollment, Outbox, Student, utcnow
from .preferences import is_admin


SLOT_LIMIT = 144
SLOT_LINE = re.compile(r"([0-9]{4}/[0-9]{2}/[0-9]{2})\s+([0-9]{2}:[0-9]{2})\s*[-–]\s*([0-9]{2}:[0-9]{2})(?:\s+([0-9]{1,4}))?")


def parse_consultation_slots(text, automatic, now):
    lines = excel.normalize(text).splitlines()
    if not 1 <= len(lines) <= (1 if automatic else SLOT_LIMIT):
        raise ValueError
    slots = []
    for line in lines:
        match = SLOT_LINE.fullmatch(line.strip())
        if match is None or bool(match[4]) != automatic:
            raise ValueError
        day, start, end, duration = match.groups()
        start, end = parse_start(f"{day} {start}"), parse_start(f"{day} {end}")
        if start <= now or start >= end:
            raise ValueError
        if automatic:
            minutes = int(duration)
            span = (end - start) // timedelta(minutes=1)
            if not minutes or span % minutes or span // minutes > SLOT_LIMIT:
                raise ValueError
            step = timedelta(minutes=minutes)
            slots.extend((start + index * step, start + (index + 1) * step) for index in range(span // minutes))
        else:
            slots.append((start, end))
    slots.sort()
    if any(next_start < end for (_, end), (next_start, _) in zip(slots, slots[1:])):
        raise ValueError
    return slots


def slot_label(start, end):
    start, end = utc(start).astimezone(TEHRAN), utc(end).astimezone(TEHRAN)
    day = jdatetime.date.fromgregorian(date=start.date())
    return f"{DAYS[start.weekday()]} {day:%Y/%m/%d} | {start:%H:%M} تا {end:%H:%M}"


def guardian_students(user_id):
    return select(Student.id).join(GuardianLink, GuardianLink.student_id == Student.id).where(
        GuardianLink.guardian_id == user_id, Student.active.is_(True),
    )


def family_students(session, user_id):
    guardians, students, pending = {user_id}, set(), {user_id}
    while pending:
        linked = select(GuardianLink.student_id).where(GuardianLink.guardian_id.in_(pending))
        found = dict(session.execute(select(Student.id, Student.bale_user_id).where(
            Student.active.is_(True), Student.id.not_in(students),
            or_(Student.id.in_(linked), Student.bale_user_id.in_(pending)),
        )).all())
        if not found:
            break
        students.update(found)
        related = set(session.scalars(select(GuardianLink.guardian_id).where(GuardianLink.student_id.in_(found))))
        related.update(account_id for account_id in found.values() if account_id is not None)
        pending = related - guardians
        guardians.update(pending)
    return students


def family_reservation(session, consultation_id, user_id, students):
    claims = select(ConsultationFamilyClaim.slot_id).where(
        ConsultationFamilyClaim.consultation_id == consultation_id, ConsultationFamilyClaim.student_id.in_(students),
    )
    return session.scalar(select(ConsultationSlot).join(ConsultationBooking).where(
        ConsultationBooking.consultation_id == consultation_id,
        or_(ConsultationBooking.booked_by == user_id, ConsultationSlot.id.in_(claims)),
    ).order_by(ConsultationSlot.starts_at).limit(1))


def consultation_report(session, consultation):
    book = Workbook()
    sheet = book.active
    sheet.title = "نوبت‌های مشاوره"
    sheet.sheet_view.rightToLeft = True
    sheet.freeze_panes = "A2"
    for column, width in zip("ABCDEFG", (30, 25, 45, 20, 40, 22, 38)):
        sheet.column_dimensions[column].width = width
    _append(sheet, ("عنوان مشاوره", "کلاس", "نوبت (تهران)", "وضعیت", "دانش‌آموزان", "شناسه حساب رزروکننده", "زمان رزرو (تهران)"))
    classroom = session.get(Classroom, consultation.classroom_id)
    rows = session.execute(select(ConsultationSlot, ConsultationBooking).outerjoin(ConsultationBooking).where(
        ConsultationSlot.consultation_id == consultation.id,
    ).order_by(ConsultationSlot.starts_at))
    now = utcnow()
    for slot, booking in rows:
        _append(sheet, (consultation.title, classroom.title, slot_label(slot.starts_at, slot.ends_at),
                        "رزرو شده" if booking else "گذشته" if utc(slot.starts_at) <= now else "آزاد",
                        booking.student_names if booking else "", str(booking.booked_by) if booking else "",
                        jdatetime.datetime.fromgregorian(datetime=utc(booking.booked_at).astimezone(TEHRAN))
                        .strftime("%Y/%m/%d %H:%M") if booking else ""))
    sheet.auto_filter.ref = sheet.dimensions
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def valid_consultation_delivery(session, message, owner_id):
    _, actor_id, consultation_id, _ = message.dedupe_key.split(":")
    consultation = session.get(Consultation, int(consultation_id))
    return (consultation is not None and enabled(session, "consultations") and enabled(session, "excel")
            and class_access(session, owner_id, int(actor_id), consultation.classroom_id, "consultation"))


class ConsultationsMixin:
    def consultation_staff(self, session, chat_id, user_id, state, data):
        if not self.allow(session, chat_id, ["consultations"]):
            return
        _, action, *args = data.split(":")
        admin = is_admin(session, user_id, self.config.admin_id)
        pending = state.data
        if action == "classes" and admin and (values := identifiers(args, ID_MAX)) is not None:
            self.set_state(state)
            self.education_page(session, chat_id, select(Classroom).order_by(Classroom.title, Classroom.id),
                                values[0], "cs:classes", "برای مدیریت مشاوره، کلاس را انتخاب کنید.",
                                lambda room: (room.title, f"cs:list:{room.id}:0"))
            return
        if action == "list" and (values := identifiers(args, ID_MAX, ID_MAX)) is not None:
            classroom_id, page = values
            if self.education_allowed(session, chat_id, user_id, classroom_id, "consultation"):
                self.set_state(state)
                classroom = session.get(Classroom, classroom_id)
                self.education_page(session, chat_id, select(Consultation).where(
                    Consultation.classroom_id == classroom_id,
                ).order_by(Consultation.id.desc()), page, f"cs:list:{classroom_id}",
                    f"{CONSULTATION_BUTTON} | {classroom.title}",
                    lambda item: (item.title, f"cs:view:{item.id}"),
                    [("➕ تعریف مشاوره", f"cs:new:{classroom_id}"), ("کلاس‌ها", "cs:classes:0")]
                    if admin else [("بازگشت به کلاس", f"ed:class:{classroom_id}")])
            return
        if action in ("view", "export") and (values := identifiers(args, ID_MAX)) is not None:
            consultation = session.get(Consultation, values[0])
            if consultation is None:
                self.say(session, chat_id, "مشاوره پیدا نشد.")
                return
            if not self.education_allowed(session, chat_id, user_id, consultation.classroom_id, "consultation"):
                return
            if action == "view":
                rows = [[("📥 گزارش اکسل نوبت‌ها", f"cs:export:{consultation.id}")]] if enabled(session, "excel") else []
                self.say(session, chat_id, f"{consultation.title}\n\n{consultation.description}", keyboard(
                    rows + [[("بازگشت", f"cs:list:{consultation.classroom_id}:0")]]))
            elif self.allow(session, chat_id, ["excel"]):
                session.add(Outbox(method="sendDocument", payload={
                    "chat_id": chat_id, "caption": "گزارش نوبت‌های مشاوره",
                    "_file": {"name": f"consultation-{consultation.id}.xlsx",
                              "content": base64.b64encode(consultation_report(session, consultation)).decode("ascii")},
                }, dedupe_key=f"csreport:{user_id}:{consultation.id}:{secrets.token_hex(12)}"))
                self.audit(session, user_id, "consultation_export", consultation_id=consultation.id)
            return
        if not admin:
            self.say(session, chat_id, "فقط ادمین می‌تواند مشاوره تعریف کند.")
            return
        if action == "new" and (values := identifiers(args, ID_MAX)) is not None and session.get(Classroom, values[0]):
            self.set_state(state, mode="consultation_title", draft={"classroom_id": values[0]})
            self.say(session, chat_id, "عنوان مشاوره را بفرستید (مثلاً مشاوره تحصیلی).",
                     keyboard([[("لغو", "cs:cancel")]]))
        elif action == "cancel" and not args and pending.get("mode", "").startswith("consultation_"):
            self.set_state(state)
            self.say(session, chat_id, "تعریف مشاوره لغو شد.")
        elif action == "input" and len(args) == 2 and args[0] in ("auto", "manual") and (
                pending.get("mode") == "consultation_method" and args[1] == pending.get("nonce")):
            self.set_state(state, mode="consultation_slots", draft=pending["draft"], automatic=args[0] == "auto")
            self.consultation_slot_prompt(session, chat_id, args[0] == "auto")
        elif action == "save" and pending.get("mode") == "consultation_preview" and args == [pending["nonce"]]:
            self.consultation_save(session, chat_id, user_id, state, pending["draft"])

    def consultation_slot_prompt(self, session, chat_id, automatic, invalid=False):
        tomorrow = jdatetime.date.fromgregorian(date=(utcnow().astimezone(TEHRAN) + timedelta(days=1)).date())
        day = tomorrow.strftime("%Y/%m/%d")
        if automatic:
            prompt = ("تاریخ شمسی، ساعت شروع و پایان، و مدت هر نوبت به دقیقه را در یک خط بفرستید.\n"
                      f"نمونه: {day} 17:00-18:00 10\n"
                      "بازه باید دقیقاً بر مدت نوبت‌ها تقسیم شود.")
        else:
            prompt = ("هر نوبت را در یک خط با تاریخ شمسی، ساعت شروع و پایان بفرستید. نوبت‌ها نباید هم‌پوشانی داشته باشند.\n"
                      f"نمونه:\n{day} 17:00-17:10\n{day} 17:30-17:50")
        self.say(session, chat_id, ("زمان‌بندی معتبر نیست. فقط زمان‌های آینده را وارد کنید.\n\n" if invalid else "")
                 + prompt + f"\nساعت تهران | حداکثر {SLOT_LIMIT} نوبت",
                 keyboard([[("لغو", "cs:cancel")]]))

    def consultation_message(self, session, chat_id, user_id, state, text):
        if not is_admin(session, user_id, self.config.admin_id) or not self.allow(session, chat_id, ["consultations"]):
            self.set_state(state)
            return
        pending = state.data
        draft = dict(pending["draft"])
        mode = pending["mode"]
        cancel = keyboard([[("لغو", "cs:cancel")]])
        if mode == "consultation_title":
            title = " ".join(text.split())
            if not 1 <= len(title) <= 80 or "\0" in title:
                self.say(session, chat_id, "عنوان باید بین ۱ تا ۸۰ نویسه باشد.", cancel)
                return
            self.set_state(state, mode="consultation_description", draft={**draft, "title": title})
            self.say(session, chat_id, "توضیح مشاوره را بفرستید تا خانواده‌ها بدانند برای چیست.", cancel)
        elif mode == "consultation_description":
            if not 1 <= len(text) <= 1000 or "\0" in text:
                self.say(session, chat_id, "توضیح باید بین ۱ تا ۱۰۰۰ نویسه باشد.", cancel)
                return
            nonce = secrets.token_hex(8)
            self.set_state(state, mode="consultation_method", draft={**draft, "description": text}, nonce=nonce)
            self.say(session, chat_id, "نوبت‌ها چطور ساخته شوند؟", keyboard([
                [("تقسیم خودکار یک بازه", f"cs:input:auto:{nonce}")], [("ورود دستی نوبت‌ها", f"cs:input:manual:{nonce}")],
                [("لغو", "cs:cancel")],
            ]))
        elif mode == "consultation_slots":
            automatic = pending["automatic"]
            try:
                slots = parse_consultation_slots(text, automatic, utcnow())
            except (ValueError, OverflowError):
                self.consultation_slot_prompt(session, chat_id, automatic, invalid=True)
                return
            nonce = secrets.token_hex(8)
            draft["slots"] = [(start.isoformat(), end.isoformat()) for start, end in slots]
            self.set_state(state, mode="consultation_preview", draft=draft, nonce=nonce)
            preview = [slot_label(*slot) for slot in slots[:8]]
            if len(slots) > len(preview):
                preview += [f"… تا {slot_label(*slots[-1])}"]
            self.say(session, chat_id, f"انتشار «{draft['title']}» برای خانواده‌های کلاس؟\n\n{draft['description']}"
                     f"\n\nتعداد نوبت: {len(slots)}\n" + "\n".join(preview),
                     keyboard([[("انتشار", f"cs:save:{nonce}"), ("لغو", "cs:cancel")]]))
        else:
            self.say(session, chat_id, "برای ادامه یکی از دکمه‌های پیام قبلی را بزنید.", cancel)

    def consultation_save(self, session, chat_id, user_id, state, draft):
        self.set_state(state)
        slots = [(datetime.fromisoformat(start), datetime.fromisoformat(end)) for start, end in draft["slots"]]
        if slots[0][0] <= utcnow() or session.get(Classroom, draft["classroom_id"]) is None:
            self.say(session, chat_id, "کلاس یا زمان نوبت‌ها دیگر معتبر نیست. مشاوره را دوباره تعریف کنید.")
            return
        consultation = Consultation(classroom_id=draft["classroom_id"], title=draft["title"],
                                    description=draft["description"])
        session.add(consultation)
        session.flush()
        session.add_all(ConsultationSlot(consultation_id=consultation.id, starts_at=start, ends_at=end)
                        for start, end in slots)
        self.audit(session, user_id, "consultation_create", consultation_id=consultation.id)
        self.say(session, chat_id, "✅ مشاوره منتشر شد. خانواده‌های این کلاس می‌توانند نوبت بگیرند.",
                 keyboard([[("مشاهده", f"cs:view:{consultation.id}")]]))

    def consultation_family(self, session, chat_id, user_id, state, data):
        if not self.allow(session, chat_id, ["consultations"]):
            return
        account = session.get(GuardianAccount, user_id)
        guardian = account is not None and account.chat_id == chat_id
        student = bound_student(session, user_id, chat_id)
        if not guardian and student is None:
            self.say(session, chat_id, "برای رزرو مشاوره، ابتدا با حساب دانش‌آموز وارد بات شوید.")
            return
        _, action, *args = data.split(":")
        now = utcnow()
        eligible = []
        if student is not None:
            eligible.append(Enrollment.student_id == student.id)
        if guardian:
            eligible.append(Enrollment.student_id.in_(guardian_students(user_id)))
        classrooms = select(Enrollment.classroom_id).where(or_(*eligible))
        consultations = select(Consultation).where(Consultation.classroom_id.in_(classrooms))
        back = ("بازگشت به مشاوره‌ها", "cf:list:0")
        if action in ("list", "slots") and state.data.get("mode") == "family_consultation_confirm":
            self.set_state(state)
        if action == "list" and (values := identifiers(args, ID_MAX)) is not None:
            self.education_page(session, chat_id, consultations.where(Consultation.id.in_(
                select(ConsultationSlot.consultation_id).where(ConsultationSlot.starts_at > now),
            )).order_by(Consultation.id.desc()), values[0], "cf:list", CONSULTATION_BUTTON,
                lambda item: (f"{item.title} | {session.get(Classroom, item.classroom_id).title}"[:80],
                              f"cf:slots:{item.id}:0"))
            return
        if action not in ("slots", "pick", "book") or len(args) != (3 if action == "book" else 2):
            return
        values = identifiers(args[:2], ID_MAX, ID_MAX)
        if values is None:
            return
        consultation = session.scalar(consultations.where(Consultation.id == values[0]))
        if consultation is None:
            self.say(session, chat_id, "این مشاوره برای شما در دسترس نیست.")
            return
        detail = f"{consultation.title}\n{consultation.description}\n\n"
        students = family_students(session, user_id)
        reserved = family_reservation(session, consultation.id, user_id, students)
        if reserved is not None:
            self.say(session, chat_id, detail + "✅ نوبت ثبت‌شده خانواده:\n"
                     + slot_label(reserved.starts_at, reserved.ends_at), keyboard([[back]]))
            return
        available = select(ConsultationSlot).where(
            ConsultationSlot.consultation_id == consultation.id, ConsultationSlot.starts_at > now,
            ~select(ConsultationBooking.slot_id).where(ConsultationBooking.slot_id == ConsultationSlot.id).exists(),
        )
        if action == "slots":
            self.education_page(session, chat_id, available.order_by(ConsultationSlot.starts_at), values[1],
                                f"cf:slots:{consultation.id}", detail + "نوبت‌های آزاد (ساعت تهران)",
                                lambda slot: (slot_label(slot.starts_at, slot.ends_at),
                                              f"cf:pick:{consultation.id}:{slot.id}"), [back])
            return
        back = ("بازگشت به نوبت‌ها", f"cf:slots:{consultation.id}:0")
        slot = session.scalar(available.where(ConsultationSlot.id == values[1]))
        if slot is None:
            self.say(session, chat_id, "این نوبت دیگر آزاد نیست. نوبت‌های آزاد را دوباره ببینید.", keyboard([[back]]))
            return
        label = slot_label(slot.starts_at, slot.ends_at)
        if action == "pick":
            nonce = secrets.token_hex(8)
            self.set_state(state, mode="family_consultation_confirm", target=list(values), nonce=nonce)
            self.say(session, chat_id, f"{consultation.title}\n{label}\n\nاین نوبت برای خانواده ثبت شود؟",
                     keyboard([[("تأیید رزرو", f"cf:book:{consultation.id}:{slot.id}:{nonce}")], [back]]))
        elif (state.data.get("mode") == "family_consultation_confirm" and state.data.get("target") == list(values)
              and args[2] == state.data.get("nonce")):
            self.set_state(state)
            self.consultation_book(session, chat_id, user_id, consultation, slot, students, label, back)
        else:
            self.say(session, chat_id, "این تأیید معتبر نیست. دوباره یک نوبت را انتخاب کنید.", keyboard([[back]]))

    def consultation_book(self, session, chat_id, user_id, consultation, slot, students, label, back):
        names = session.scalars(select(Student.full_name).join(Enrollment).where(
            Student.id.in_(students), Enrollment.classroom_id == consultation.classroom_id,
        ).order_by(Student.full_name, Student.id))
        names = "، ".join(names)
        try:
            with session.begin_nested():
                session.add(ConsultationBooking(slot_id=slot.id, consultation_id=consultation.id,
                                                booked_by=user_id, student_names=names))
                session.flush()
                session.add_all(ConsultationFamilyClaim(consultation_id=consultation.id, student_id=student_id,
                                                        slot_id=slot.id) for student_id in sorted(students))
        except IntegrityError:
            if (session.get(ConsultationBooking, slot.id) is None
                    and family_reservation(session, consultation.id, user_id, students) is None):
                raise
            self.say(session, chat_id, "نوبت آزاد نیست یا خانواده قبلاً رزرو کرده است. نوبت‌ها را دوباره ببینید.",
                     keyboard([[back]]))
            return
        self.audit(session, user_id, "consultation_book", consultation_id=consultation.id, slot_id=slot.id)
        self.say(session, chat_id, f"✅ نوبت «{consultation.title}» ثبت شد.\n{label} (تهران)", keyboard([[back]]))
