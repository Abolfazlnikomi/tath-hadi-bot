import base64
import re
import secrets
from datetime import datetime, timedelta, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

import jdatetime
from openpyxl import Workbook
from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload

from . import excel
from .api import BaleError
from .features import enabled
from .models import Classroom, Meeting, MeetingInvite, Outbox, Student, utcnow
from .service import members


TEHRAN = ZoneInfo("Asia/Tehran")
DAYS = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")
RESPONSES = {"yes": "شرکت می‌کنم", "no": "شرکت نمی‌کنم", None: "بدون پاسخ"}
PAGE_SIZE = 8


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def parse_start(text):
    match = re.fullmatch(r"([0-9]{4})/([0-9]{2})/([0-9]{2})\s+([0-9]{2}):([0-9]{2})",
                         excel.normalize(text))
    if not match:
        raise ValueError
    year, month, day, hour, minute = map(int, match.groups())
    return jdatetime.datetime(year, month, day, hour, minute).togregorian().replace(tzinfo=TEHRAN).astimezone(timezone.utc)


def describe(meeting):
    local = utc(meeting.starts_at).astimezone(TEHRAN)
    jalali = jdatetime.date.fromgregorian(date=local.date())
    return (f"موضوع: {meeting.topic}\nکلاس: {meeting.classroom.title}\n"
            f"زمان: {DAYS[local.weekday()]} {jalali:%Y/%m/%d}، ساعت {local:%H:%M}\nمکان: {meeting.location}")


def keyboard(rows):
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


def queue_notice(session, meeting, invite, student, kind):
    detail = describe(meeting)
    if kind == "notice":
        registered = jdatetime.date.fromgregorian(date=utc(meeting.created_at).astimezone(TEHRAN).date())
        text = f"📅 در تاریخ {registered:%Y/%m/%d} جلسه‌ای برای شما ثبت شد.\n\n{detail}"
    else:
        text = f"{'🔔 یادآوری سه روز مانده به جلسه' if kind == 'three' else '🔔 یادآوری یک روز مانده به جلسه'}\n\n{detail}"
    payload = {"chat_id": student.chat_id, "text": text}
    if kind == "three":
        payload["reply_markup"] = keyboard([[(RESPONSES[choice], f"mr:{meeting.id}:{choice}") for choice in ("yes", "no")]])
    revision = f":{meeting.revision}" if meeting.revision > 1 else ""
    session.add(Outbox(method="sendMessage", payload=payload,
                       dedupe_key=f"meeting:{meeting.id}:{student.id}:{kind}{revision}"))
    setattr(invite, f"{kind}_queued", True)


def schedule_reminders(session, now=None):
    if not enabled(session, "meetings"):
        return
    now = now or utcnow()
    invites = session.scalars(select(MeetingInvite).join(MeetingInvite.meeting).join(MeetingInvite.student).where(
        Student.active.is_(True), Student.chat_id.is_not(None), Student.bale_user_id.is_not(None),
        Meeting.starts_at > now, Meeting.cancelled.is_(False),
        or_(MeetingInvite.notice_queued.is_(False),
            (MeetingInvite.three_queued.is_(False) & (Meeting.starts_at <= now + timedelta(days=3))),
            (MeetingInvite.one_queued.is_(False) & (Meeting.starts_at <= now + timedelta(days=1)))),
    ).options(joinedload(MeetingInvite.meeting).joinedload(Meeting.classroom), joinedload(MeetingInvite.student)))
    for invite in invites:
        meeting = invite.meeting
        if not invite.notice_queued:
            queue_notice(session, meeting, invite, invite.student, "notice")
        remaining = utc(meeting.starts_at) - now
        if remaining <= timedelta(days=1):
            if not invite.one_queued:
                queue_notice(session, meeting, invite, invite.student, "one")
            invite.three_queued = True
        elif remaining <= timedelta(days=3) and not invite.three_queued:
            queue_notice(session, meeting, invite, invite.student, "three")


def valid_delivery(session, message, now):
    _, meeting_id, student_id, kind, *revision = message.dedupe_key.split(":")
    student = session.get(Student, int(student_id))
    meeting = session.get(Meeting, int(meeting_id))
    if not (enabled(session, "meetings") and student and student.active and student.bale_user_id is not None
            and student.chat_id == message.payload.get("chat_id") and meeting and not meeting.cancelled
            and meeting.revision == (int(revision[0]) if revision else 1)):
        return False
    expiry = utc(meeting.starts_at) - (timedelta(days=1) if kind == "three" else timedelta())
    return now < expiry


def response_workbook(session, meeting):
    book = Workbook()
    book.remove(book.active)
    sheets = {}
    for response, title in RESPONSES.items():
        sheet = sheets[response] = book.create_sheet(title)
        sheet.sheet_view.rightToLeft = True
        sheet.append(["کد ملی", "نام و نام خانوادگی", "کلاس", "موضوع", "پاسخ"])
        sheet.freeze_panes = "A2"
        for column in "ABCDE":
            sheet.column_dimensions[column].width = 28
    for invite in session.scalars(select(MeetingInvite).join(Student).where(
            MeetingInvite.meeting_id == meeting.id).order_by(Student.full_name, Student.id)
            .options(joinedload(MeetingInvite.student))):
        sheet = sheets[invite.response]
        sheet.append([invite.student.national_id, invite.student.full_name, meeting.classroom.title,
                      meeting.topic, RESPONSES[invite.response]])
        for cell in sheet[sheet.max_row]:
            cell.data_type = "s"
            cell.number_format = "@"
    for sheet in sheets.values():
        sheet.auto_filter.ref = sheet.dimensions
    result = BytesIO()
    book.save(result)
    return result.getvalue()


class MeetingsMixin:
    def meeting_document(self, session, chat_id, content, name, caption):
        self.queue(session, "sendDocument", {"chat_id": chat_id, "caption": caption,
                   "_file": {"name": name, "content": base64.b64encode(content).decode("ascii")}})

    def meeting_list(self, session, chat_id, page=0, student=None):
        query = select(Meeting)
        if student is not None:
            query = query.join(MeetingInvite).where(MeetingInvite.student_id == student.id, Meeting.cancelled.is_(False))
        items = list(session.scalars(query.order_by(Meeting.starts_at.desc(), Meeting.id.desc())
                                     .offset(page * PAGE_SIZE).limit(PAGE_SIZE + 1)))
        rows = [[("📅 جلسات والدین", "pm:list:0")], [("➕ ثبت جلسه برای دانش‌آموزان", "mt:classes:0")]] if student is None else []
        prefix = "mt:list" if student is None else "my:page"
        if student is None:
            rows.extend([(f"{'لغوشده | ' if item.cancelled else ''}{item.topic[:62]}", f"mt:view:{item.id}")] for item in items[:PAGE_SIZE])
        nav = []
        if page:
            nav.append(("قبلی", f"{prefix}:{page - 1}"))
        if len(items) > PAGE_SIZE:
            nav.append(("بعدی", f"{prefix}:{page + 1}"))
        if nav:
            rows.append(nav)
        if student is None:
            text = "جلسات ثبت‌شده" if items else "هنوز جلسه‌ای ثبت نشده است."
        else:
            text = "\n\n".join(describe(item) + "\nپاسخ شما: " +
                                 RESPONSES[session.get(MeetingInvite, (item.id, student.id)).response]
                                 for item in items[:PAGE_SIZE]) or "جلسه‌ای برای شما ثبت نشده است."
        self.say(session, chat_id, text, keyboard(rows) if rows else None)

    def registered_participant(self, session, chat_id, user_id, data):
        if not self.allow(session, chat_id, ["meetings"]):
            return
        student = session.scalar(select(Student).where(Student.active.is_(True),
                                  Student.bale_user_id == user_id, Student.chat_id == chat_id))
        if not student:
            self.say(session, chat_id, "ابتدا با حساب دانش‌آموز وارد شوید.")
            return
        page = re.fullmatch(r"my:page:([0-9]{1,9})", data)
        if page:
            self.meeting_list(session, chat_id, int(page[1]), student)
            return
        answer = re.fullmatch(r"mr:([0-9]{1,18}):(yes|no)", data)
        if not answer:
            return
        invite = session.get(MeetingInvite, (int(answer[1]), student.id))
        now = utcnow()
        if not invite or invite.meeting.cancelled or utc(invite.meeting.starts_at) <= now:
            self.say(session, chat_id, "این دعوت در دسترس نیست یا زمان پاسخ به پایان رسیده است.")
            return
        if invite.response != answer[2]:
            invite.response = answer[2]
            invite.responded_at = now
        self.say(session, chat_id, f"✅ پاسخ شما ثبت شد: {RESPONSES[answer[2]]}")

    def meeting_preview(self, session, chat_id, state, audience, codes=None):
        draft = state.data["draft"]
        roster = self.meeting_roster(session, draft["classroom_id"], codes)
        nonce = secrets.token_hex(8)
        self.set_state(state, mode="meeting_confirm", draft=draft, audience=audience, codes=codes, nonce=nonce)
        proposed = Meeting(classroom=session.get(Classroom, draft["classroom_id"]), topic=draft["topic"],
                           location=draft["location"], starts_at=datetime.fromisoformat(draft["starts_at"]))
        linked = sum(student.chat_id is not None and student.bale_user_id is not None for student in roster)
        self.say(session, chat_id, f"{describe(proposed)}\n\nدعوت‌شده: {len(roster)} نفر | متصل به بله: {linked} نفر",
                 keyboard([[("تأیید و ارسال", f"mt:save:{nonce}"), ("لغو", "cancel")]]))

    @staticmethod
    def meeting_roster(session, classroom_id, codes):
        roster = members(session, classroom_id)
        if codes is not None:
            required = set(codes)
            roster = [student for student in roster if student.national_id in required]
            missing = len(required) - len(roster)
            if missing:
                raise ValueError(f"{missing} کد ملی مربوط به اعضای فعال این کلاس نیست.")
        if not roster:
            raise ValueError("هیچ عضو فعالی برای دعوت وجود ندارد.")
        return roster

    def meeting_admin_callback(self, session, chat_id, user_id, state, data):
        if not self.allow(session, chat_id, ["meetings"]):
            return
        match = re.fullmatch(r"mt:([a-z]+):([0-9a-f]{1,18})", data)
        if not match:
            return
        action, value = match.groups()
        if action in ("list", "classes", "choose", "view", "export", "edit", "delete") and not value.isdigit():
            return
        if action in ("list", "classes") and len(value) > 9:
            return
        mode = state.data.get("mode")
        if action == "list":
            self.set_state(state)
            self.meeting_list(session, chat_id, int(value))
        elif action == "classes":
            self.set_state(state)
            page = int(value)
            classes = list(session.scalars(select(Classroom).order_by(Classroom.title, Classroom.id)
                                           .offset(page * PAGE_SIZE).limit(PAGE_SIZE + 1)))
            rows = [[(room.title[:62], f"mt:choose:{room.id}")] for room in classes[:PAGE_SIZE]]
            if page:
                rows.append([("قبلی", f"mt:classes:{page - 1}")])
            if len(classes) > PAGE_SIZE:
                rows.append([("بعدی", f"mt:classes:{page + 1}")])
            rows.append([("بازگشت", "mt:list:0")])
            self.say(session, chat_id, "کلاس جلسه را انتخاب کنید." if classes else "کلاسی یافت نشد.", keyboard(rows))
        elif action == "choose":
            if not session.get(Classroom, int(value)):
                self.say(session, chat_id, "کلاس یافت نشد.")
                return
            self.set_state(state, mode="meeting_topic", draft={"classroom_id": int(value)})
            self.say(session, chat_id, "موضوع جلسه را بفرستید (حداکثر ۲۰۰ نویسه).")
        elif action == "audience" and mode == "meeting_audience" and value in ("a", "e"):
            draft = state.data["draft"]
            try:
                if value == "a":
                    self.meeting_preview(session, chat_id, state, "class")
                elif self.allow(session, chat_id, ["excel"]):
                    self.meeting_document(session, chat_id, excel.workbook("meeting_invites", classroom_id=draft["classroom_id"]),
                                          "meeting-invite-template.xlsx", "کد ملی اعضای فعال همین کلاس را در ستون متنی وارد کنید و فایل را بفرستید.")
                    self.set_state(state, mode="meeting_upload", draft=draft)
            except ValueError as error:
                self.say(session, chat_id, str(error))
        elif action == "save" and mode == "meeting_confirm" and value == state.data["nonce"]:
            draft, codes = state.data["draft"], state.data["codes"]
            if codes is not None and not self.allow(session, chat_id, ["excel"]):
                return
            now = utcnow()
            if datetime.fromisoformat(draft["starts_at"]) <= now:
                self.set_state(state)
                self.say(session, chat_id, "زمان جلسه گذشته است؛ جلسه را دوباره تعریف کنید.")
                return
            try:
                roster = self.meeting_roster(session, draft["classroom_id"], codes)
            except ValueError as error:
                self.say(session, chat_id, str(error))
                return
            meeting = Meeting(**{**draft, "starts_at": datetime.fromisoformat(draft["starts_at"])}, created_at=now)
            session.add(meeting)
            session.flush()
            queued = 0
            for student in roster:
                invite = MeetingInvite(meeting_id=meeting.id, student_id=student.id)
                session.add(invite)
                if student.chat_id is not None and student.bale_user_id is not None:
                    queue_notice(session, meeting, invite, student, "notice")
                    queued += 1
            self.audit(session, user_id, "create_meeting", meeting_id=meeting.id, recipients=len(roster),
                       audience=state.data["audience"])
            self.set_state(state)
            self.say(session, chat_id, f"✅ جلسه ثبت شد. اعلان {queued} نفر در صف است. "
                     f"{len(roster) - queued} نفر هنوز به بله متصل نیستند.",
                     keyboard([[("مشاهده جلسه", f"mt:view:{meeting.id}"), ("همه جلسات", "mt:list:0")]]))
        elif action == "change" and mode == "meeting_change_confirm" and value == state.data["nonce"]:
            draft = state.data["draft"]
            self.set_state(state)
            meeting = session.get(Meeting, draft["id"])
            if not meeting or meeting.cancelled or meeting.revision != draft["revision"]:
                self.say(session, chat_id, "جلسه تغییر کرده یا دیگر در دسترس نیست.")
                return
            if draft.get("cancelled"):
                meeting.cancelled = True
            else:
                start = datetime.fromisoformat(draft["starts_at"])
                if start <= utcnow():
                    self.say(session, chat_id, "زمان جدید باید در آینده باشد.")
                    return
                meeting.topic, meeting.location, meeting.starts_at = draft["topic"], draft["location"], start
                for invite in session.scalars(select(MeetingInvite).where(MeetingInvite.meeting_id == meeting.id)):
                    invite.notice_queued = invite.three_queued = invite.one_queued = False
            meeting.revision += 1
            self.audit(session, user_id, "cancel_meeting" if meeting.cancelled else "edit_meeting", meeting_id=meeting.id)
            self.say(session, chat_id, "✅ تغییرات جلسه ثبت شد.")
        elif action in ("view", "export", "edit", "delete"):
            meeting = session.get(Meeting, int(value))
            if not meeting:
                self.say(session, chat_id, "جلسه یافت نشد.")
            elif action == "view":
                rows = [[("بازگشت", "mt:list:0")]]
                if enabled(session, "excel"):
                    rows.insert(0, [("دریافت اکسل پاسخ‌ها", f"mt:export:{meeting.id}")])
                if not meeting.cancelled:
                    rows.insert(0, [("ویرایش", f"mt:edit:{meeting.id}"), ("حذف / لغو", f"mt:delete:{meeting.id}")])
                self.say(session, chat_id, describe(meeting) + ("\nلغوشده" if meeting.cancelled else ""), keyboard(rows))
            elif action == "export" and self.allow(session, chat_id, ["excel"]):
                self.meeting_document(session, chat_id, response_workbook(session, meeting),
                                      f"meeting-{meeting.id}-responses.xlsx", "گزارش پاسخ دعوت‌شدگان")
            elif action in ("edit", "delete") and not meeting.cancelled:
                draft = {"id": meeting.id, "revision": meeting.revision}
                if action == "delete":
                    self.meeting_change_preview(session, chat_id, state, {**draft, "cancelled": True}, "این جلسه لغو شود؟")
                else:
                    from .parent_meetings import time_prompt

                    self.set_state(state, mode="meeting_change", draft=draft)
                    self.say(session, chat_id, "موضوع، مکان و زمان جدید را در سه خط بفرستید.\n" + time_prompt())
        else:
            self.say(session, chat_id, "این گزینه منقضی شده است؛ دوباره از منوی جلسات اقدام کنید.")

    def meeting_change_preview(self, session, chat_id, state, draft, prompt):
        nonce = secrets.token_hex(8)
        self.set_state(state, mode="meeting_change_confirm", draft=draft, nonce=nonce)
        self.say(session, chat_id, prompt, keyboard([[("تأیید", f"mt:change:{nonce}"), ("لغو", "cancel")]]))

    def meeting_admin_message(self, session, chat_id, state, message, text):
        mode = state.data["mode"]
        draft = dict(state.data["draft"])
        if not self.allow(session, chat_id, ["meetings"] + (["excel"] if mode == "meeting_upload" else [])):
            return
        if mode == "meeting_change":
            from .parent_meetings import time_prompt

            fields = [field.strip() for field in text.splitlines()]
            try:
                if len(fields) != 3 or not all(1 <= len(field) <= 200 and "\0" not in field for field in fields[:2]):
                    raise ValueError
                start = parse_start(fields[2])
                if start <= utcnow():
                    raise ValueError
            except (ValueError, OverflowError):
                self.say(session, chat_id, "سه خط معتبر بفرستید.\n" + time_prompt())
                return
            self.meeting_change_preview(session, chat_id, state,
                                        {**draft, "topic": fields[0], "location": fields[1], "starts_at": start.isoformat()},
                                        f"تغییرات تأیید شود؟\n{fields[0]}\n{fields[1]}\n{fields[2]}")
        elif mode in ("meeting_topic", "meeting_location"):
            if not 1 <= len(text) <= 200:
                self.say(session, chat_id, "متن باید بین ۱ تا ۲۰۰ نویسه باشد.")
                return
            if mode == "meeting_topic":
                draft["topic"] = text
                self.set_state(state, mode="meeting_location", draft=draft)
                self.say(session, chat_id, "مکان جلسه را بفرستید.")
            else:
                draft["location"] = text
                self.set_state(state, mode="meeting_time", draft=draft)
                from .parent_meetings import time_prompt

                self.say(session, chat_id, time_prompt())
        elif mode == "meeting_time":
            try:
                start = parse_start(text)
                if start <= utcnow():
                    raise ValueError
            except (ValueError, OverflowError):
                from .parent_meetings import time_prompt

                self.say(session, chat_id, "تاریخ یا ساعت معتبر نیست.\n" + time_prompt())
                return
            draft["starts_at"] = start.isoformat()
            self.set_state(state, mode="meeting_audience", draft=draft)
            rows = [[("کل کلاس", "mt:audience:a")]]
            if enabled(session, "excel"):
                rows.append([("انتخاب افراد با اکسل", "mt:audience:e")])
            self.say(session, chat_id, "دعوت‌شدگان را مشخص کنید.", keyboard(rows))
        elif mode == "meeting_upload":
            document = message.get("document")
            if not document:
                self.say(session, chat_id, "فایل نمونه تکمیل‌شده با پسوند xlsx را بفرستید.")
                return
            try:
                parsed = excel.parse(self.api.download(document), "meeting_invites", draft["classroom_id"],
                                     self.config.max_excel_rows)
                self.meeting_preview(session, chat_id, state, "excel", [row["national_id"] for row in parsed["rows"]])
            except BaleError as error:
                if error.code not in (400, 403, 404):
                    raise
                self.say(session, chat_id, "دانلود فایل از بله انجام نشد؛ دوباره ارسال کنید.")
            except ValueError as error:
                self.say(session, chat_id, f"فایل پذیرفته نشد: {error}")
        else:
            self.say(session, chat_id, "برای ادامه دکمهٔ پیام قبلی را بزنید یا «لغو» را انتخاب کنید.")
