import secrets
from datetime import datetime, timedelta
from io import BytesIO

import jdatetime
from openpyxl import Workbook
from sqlalchemy import select

from .api import BaleError
from .education_access import ID_MAX, identifiers
from .education_models import GuardianAccount, GuardianLink
from .features import enabled
from .meetings import RESPONSES, TEHRAN, keyboard, parse_start, utc
from .models import Classroom, Enrollment, Outbox, Student, utcnow
from .parent_meeting_models import MeetingSection, ParentMeeting, ParentMeetingClass, ParentMeetingStage


PARENT_BUTTON = "📅 جلسات والدین"
PHOTO_LIMIT = 1024 * 1024
STAGES = {"new": "دعوت اولیه", "three": "یادآوری سه روز قبل", "one": "یادآوری یک روز قبل"}
PAGE_SIZE = 8


def local_time(value):
    return jdatetime.datetime.fromgregorian(datetime=utc(value).astimezone(TEHRAN)).strftime("%Y/%m/%d %H:%M")


def time_prompt():
    now = utcnow()
    return (f"امروز در تهران: {local_time(now)}\n"
            f"زمان آینده را به شمسی بفرستید؛ نمونهٔ فردا در همین ساعت: {local_time(now + timedelta(days=1))}")


def meeting_rooms(session, meeting_id):
    return list(session.scalars(select(Classroom).join(ParentMeetingClass).where(
        ParentMeetingClass.meeting_id == meeting_id,
    ).order_by(Classroom.id)))


def detail(session, meeting):
    titles = "، ".join(room.title for room in meeting_rooms(session, meeting.id))
    return (f"بخش: {session.get(MeetingSection, meeting.section_id).title}\nموضوع: {meeting.topic}"
            f"\nزمان: {local_time(meeting.starts_at)} (تهران)\nمکان: {meeting.location}\nکلاس‌ها: {titles}")


def eligible_links(meeting_id):
    return select(GuardianLink.guardian_id, GuardianLink.student_id).join(
        Student, Student.id == GuardianLink.student_id,
    ).join(Enrollment, Enrollment.student_id == Student.id).join(
        ParentMeetingClass, ParentMeetingClass.classroom_id == Enrollment.classroom_id,
    ).where(ParentMeetingClass.meeting_id == meeting_id, Student.active.is_(True)).distinct()


def linked_meetings(user_id):
    return select(ParentMeeting.id).join(ParentMeetingClass).join(
        Enrollment, Enrollment.classroom_id == ParentMeetingClass.classroom_id,
    ).join(Student, Student.id == Enrollment.student_id).join(GuardianLink, GuardianLink.student_id == Student.id).where(
        GuardianLink.guardian_id == user_id, Student.active.is_(True), ParentMeeting.cancelled.is_(False),
    )


def queue_parent_stage(session, meeting, guardian_id, chat_id, stage, info):
    key = (meeting.id, meeting.revision, guardian_id, stage)
    if session.get(ParentMeetingStage, key) is not None:
        return
    session.add(ParentMeetingStage(meeting_id=key[0], revision=key[1], guardian_id=key[2], stage=key[3]))
    prefix = f"pm:{meeting.id}:{meeting.revision}:{guardian_id}:{stage}"
    if stage == "new" and meeting.photo_id:
        session.add(Outbox(method="sendPhoto", dedupe_key=f"{prefix}:p",
                           payload={"chat_id": chat_id, "photo": meeting.photo_id, "caption": meeting.topic}))
    payload = {"chat_id": chat_id, "text": f"📅 {STAGES.get(stage, 'لغو جلسه')}\n\n{info}"}
    if stage != "cancel":
        payload["text"] += "\n\nدر جلسه شرکت می‌کنید؟"
        payload["reply_markup"] = keyboard([[(RESPONSES[choice], f"pf:r:{meeting.id}:{meeting.revision}:{stage}:{choice}")
                                             for choice in ("yes", "no")]])
    session.add(Outbox(method="sendMessage", payload=payload, dedupe_key=f"{prefix}:t"))


def schedule_parent_meetings(session, now=None, only=None):
    if not enabled(session, "meetings"):
        return
    now = now or utcnow()
    query = select(ParentMeeting).where(ParentMeeting.starts_at > now, ParentMeeting.cancelled.is_(False))
    if only is not None:
        query = query.where(ParentMeeting.id == only)
    for meeting in session.scalars(query):
        links = eligible_links(meeting.id).subquery()
        recipients = session.execute(select(GuardianAccount.user_id, GuardianAccount.chat_id).where(
            GuardianAccount.user_id.in_(select(links.c.guardian_id)), GuardianAccount.chat_id.is_not(None),
        )).all()
        if not recipients:
            continue
        remaining = utc(meeting.starts_at) - now
        reminder = "one" if remaining <= timedelta(days=1) else "three" if remaining <= timedelta(days=3) else None
        info = detail(session, meeting)
        for guardian_id, chat_id in recipients:
            queue_parent_stage(session, meeting, guardian_id, chat_id, "new", info)
            if reminder is not None:
                queue_parent_stage(session, meeting, guardian_id, chat_id, reminder, info)


def parent_delivery_stage(session, message, now):
    _, meeting_id, version, guardian_id, stage, _ = message.dedupe_key.split(":")
    meeting_id, version, guardian_id = map(int, (meeting_id, version, guardian_id))
    meeting = session.get(ParentMeeting, meeting_id)
    account = session.get(GuardianAccount, guardian_id)
    if (not meeting or not account or account.chat_id is None or account.chat_id != message.payload.get("chat_id")
            or meeting.revision != version or not enabled(session, "meetings")
            or session.scalar(eligible_links(meeting_id).where(GuardianLink.guardian_id == guardian_id).limit(1)) is None):
        return None
    if stage == "cancel":
        return session.get(ParentMeetingStage, (meeting_id, version, guardian_id, stage)) if meeting.cancelled else None
    expiry = utc(meeting.starts_at) - (timedelta(days=1) if stage == "three" else timedelta())
    if meeting.cancelled or now >= expiry:
        return None
    return session.get(ParentMeetingStage, (meeting_id, version, guardian_id, stage))


def parent_report(session, meeting):
    book = Workbook(write_only=True)
    overview = book.create_sheet("خلاصه جلسه")
    current = book.create_sheet("پاسخ جامع والدین")
    history = book.create_sheet("ریز مراحل و نسخه‌ها")
    for sheet in (overview, current, history):
        sheet.sheet_view.rightToLeft = True
        sheet.freeze_panes = "A2"
    from .coins import _append

    _append(overview, ("بخش و اطلاعات جلسه", "نسخه", "وضعیت"))
    _append(overview, (detail(session, meeting), meeting.revision, "لغوشده" if meeting.cancelled else "ثبت‌شده"))
    _append(current, ("شناسه ولی", "دانش‌آموزان مرتبط", "وضعیت دریافت", *STAGES.values(), "آخرین پاسخ"))
    _append(history, ("نسخه", "شناسه ولی", "مرحله", "زمان ارسال", "پاسخ", "زمان پاسخ"))
    students = {}
    links = eligible_links(meeting.id).subquery()
    for guardian_id, name in session.execute(select(links.c.guardian_id, Student.full_name).join(
            Student, Student.id == links.c.student_id).order_by(links.c.guardian_id, Student.full_name)):
        students.setdefault(guardian_id, []).append(name)
    records = {}
    for item in session.scalars(select(ParentMeetingStage).where(
            ParentMeetingStage.meeting_id == meeting.id).order_by(ParentMeetingStage.revision, ParentMeetingStage.guardian_id,
                                                                ParentMeetingStage.stage)):
        _append(history, (item.revision, str(item.guardian_id), STAGES.get(item.stage, "لغو"),
                          local_time(item.delivered_at) if item.delivered_at else "ارسال نشده",
                          RESPONSES[item.response], local_time(item.responded_at) if item.responded_at else ""))
        if item.revision == meeting.revision:
            records.setdefault(item.guardian_id, {})[item.stage] = item
    for guardian_id in sorted(students.keys() | records.keys()):
        account = session.get(GuardianAccount, guardian_id)
        steps = records.get(guardian_id, {})
        latest = max((row for row in steps.values() if row.responded_at),
                     key=lambda row: utc(row.responded_at), default=None)
        _append(current, (str(guardian_id), "، ".join(students.get(guardian_id, [])),
                          "فعال" if account and account.chat_id is not None else "غیرفعال",
                          *(RESPONSES[steps[stage].response] if stage in steps else "مرحله ثبت نشده" for stage in STAGES),
                          RESPONSES[latest.response if latest else None]))
    output = BytesIO()
    book.save(output)
    return output.getvalue()


class ParentMeetingsMixin:
    def parent_meeting_admin(self, session, chat_id, user_id, state, data):
        if not self.allow(session, chat_id, ["meetings"]):
            return
        _, action, *args = data.split(":")
        pending = state.data
        if action == "save":
            if pending.get("mode") == "parent_confirm" and args == [pending.get("nonce")]:
                self.parent_meeting_save(session, chat_id, user_id, state, pending)
            return
        limits = {"list": (ID_MAX,), "section": (), "new": (), "sections": (ID_MAX,), "pick": (ID_MAX,), "rooms": (ID_MAX,),
                  "toggle": (ID_MAX, ID_MAX), "next": (), "view": (ID_MAX,), "edit": (ID_MAX,),
                  "delete": (ID_MAX,), "export": (ID_MAX,)}
        if action not in limits:
            return
        values = identifiers(args, *limits[action])
        if values is None:
            return
        if action == "list":
            self.set_state(state)
            self.education_page(session, chat_id, select(ParentMeeting).order_by(ParentMeeting.id.desc()), values[0],
                                "pm:list", "جلسات والدین", lambda meeting: (
                                    f"{'لغوشده | ' if meeting.cancelled else ''}{meeting.topic[:65]}", f"pm:view:{meeting.id}"),
                                [("➕ ثبت جلسهٔ والدین", "pm:new"), ("➕ ایجاد بخش", "pm:section"), ("جلسات قبلی", "mt:list:0")])
        elif action == "section":
            self.set_state(state, mode="parent_section")
            self.say(session, chat_id, "نام بخش جلسه را بفرستید (مثلاً جلسه تربیتی).")
        elif action in ("new", "sections"):
            self.set_state(state)
            self.education_page(session, chat_id, select(MeetingSection).order_by(MeetingSection.id), values[0] if values else 0,
                                "pm:sections", "بخش جلسه را انتخاب کنید.",
                                lambda section: (section.title, f"pm:pick:{section.id}"), [("ایجاد بخش", "pm:section")])
        elif action == "pick":
            if session.get(MeetingSection, values[0]) is not None:
                self.set_state(state, mode="parent_rooms", draft={"section_id": values[0], "room_ids": []})
                self.parent_meeting_rooms(session, chat_id, state, 0)
        elif action in ("rooms", "toggle", "next"):
            if pending.get("mode") != "parent_rooms":
                return
            draft = dict(pending["draft"])
            if action == "toggle":
                room_id = values[0]
                if session.get(Classroom, room_id) is None:
                    return
                room_ids = set(draft["room_ids"])
                if room_id not in room_ids and len(room_ids) >= 10:
                    self.say(session, chat_id, "حداکثر ۱۰ کلاس برای هر جلسه انتخاب کنید.")
                    return
                room_ids.symmetric_difference_update({room_id})
                draft["room_ids"] = sorted(room_ids)
                self.set_state(state, mode="parent_rooms", draft=draft)
            if action == "next":
                if not draft["room_ids"]:
                    self.say(session, chat_id, "حداقل یک کلاس انتخاب کنید.")
                    return
                self.set_state(state, mode="parent_details", draft=draft)
                self.say(session, chat_id, "موضوع، مکان و زمان را در سه خط بفرستید.\n" + time_prompt()
                         + ("\nبرای نگه‌داشتن مقدار قبلی هر خط «-» بفرستید." if "id" in draft else ""))
                return
            self.parent_meeting_rooms(session, chat_id, state, values[-1])
        else:
            meeting = session.get(ParentMeeting, values[0])
            if meeting is None:
                self.say(session, chat_id, "جلسه پیدا نشد.")
                return
            if action == "export":
                if self.allow(session, chat_id, ["excel"]):
                    self.meeting_document(session, chat_id, parent_report(session, meeting),
                                          f"parent-meeting-{meeting.id}.xlsx", "گزارش جامع حضور والدین")
            elif action == "view":
                rows = [[("ویرایش", f"pm:edit:{meeting.id}"), ("حذف / لغو", f"pm:delete:{meeting.id}")]] if not meeting.cancelled else []
                if enabled(session, "excel"):
                    rows.append([("اکسل جامع پاسخ‌ها", f"pm:export:{meeting.id}")])
                stages = list(session.scalars(select(ParentMeetingStage).where(
                    ParentMeetingStage.meeting_id == meeting.id, ParentMeetingStage.revision == meeting.revision,
                )))
                counts = "\n".join(
                    f"{label}: {sum(item.response == 'yes' for item in stages if item.stage == stage)} بله، "
                    f"{sum(item.response == 'no' for item in stages if item.stage == stage)} خیر"
                    for stage, label in STAGES.items()
                )
                self.say(session, chat_id, detail(session, meeting) + ("\nلغوشده" if meeting.cancelled else f"\n\n{counts}"),
                         keyboard(rows + [[("بازگشت", "pm:list:0")]]))
            elif not meeting.cancelled:
                draft = {"id": meeting.id, "revision": meeting.revision, "section_id": meeting.section_id,
                         "room_ids": [room.id for room in meeting_rooms(session, meeting.id)], "topic": meeting.topic,
                         "location": meeting.location, "starts_at": utc(meeting.starts_at).isoformat(), "photo_id": meeting.photo_id}
                if action == "delete":
                    self.parent_meeting_preview(session, chat_id, state, draft, True)
                else:
                    self.set_state(state, mode="parent_rooms", draft=draft)
                    self.parent_meeting_rooms(session, chat_id, state, 0)

    def parent_meeting_rooms(self, session, chat_id, state, page):
        selected = state.data["draft"]["room_ids"]
        self.education_page(session, chat_id, select(Classroom).order_by(Classroom.id), page, "pm:rooms",
                            f"کلاس‌ها را انتخاب کنید | انتخاب‌شده: {len(selected)}",
                            lambda room: (f"{'✅' if room.id in selected else '⬜'} {room.title}",
                                          f"pm:toggle:{room.id}:{page}"), [("ادامه", "pm:next"), ("لغو", "cancel")])

    def parent_meeting_message(self, session, chat_id, state, message, text):
        if not self.allow(session, chat_id, ["meetings"]):
            return
        mode = state.data["mode"]
        if mode == "parent_section":
            title = " ".join(text.split())
            if not 1 <= len(title) <= 80 or "\0" in title:
                self.say(session, chat_id, "نام بخش باید بین ۱ تا ۸۰ نویسه باشد.")
                return
            if session.scalar(select(MeetingSection.id).where(MeetingSection.title == title)) is None:
                session.add(MeetingSection(title=title))
            self.set_state(state)
            self.say(session, chat_id, "بخش آماده است.", keyboard([[("ثبت جلسه", "pm:new")]]))
        elif mode == "parent_details":
            draft = dict(state.data["draft"])
            fields = [field.strip() for field in text.splitlines()]
            if len(fields) == 3:
                keys = ("topic", "location", "starts_at")
                try:
                    topic, location, start = [draft.get(key) if field == "-" else field for key, field in zip(keys, fields)]
                    if not all(value and len(value) <= 200 and "\0" not in value for value in (topic, location)) or not start:
                        raise ValueError
                    date = datetime.fromisoformat(start) if fields[2] == "-" else parse_start(start)
                    if date <= utcnow():
                        raise ValueError
                except (ValueError, OverflowError):
                    pass
                else:
                    draft.update(topic=topic, location=location, starts_at=date.isoformat())
                    self.set_state(state, mode="parent_photo", draft=draft)
                    self.say(session, chat_id, "عکس را بفرستید (حداکثر ۱ مگابایت)، یا «-» برای ادامه و «حذف عکس» برای برداشتن تصویر قبلی.")
                    return
            self.say(session, chat_id, "موضوع، مکان و زمان معتبر آینده را در سه خط بفرستید.\n" + time_prompt())
        elif mode == "parent_photo":
            draft = dict(state.data["draft"])
            if text == "حذف عکس":
                draft["photo_id"] = None
            elif text != "-":
                photos = message.get("photo")
                if not isinstance(photos, list) or not photos or any(not isinstance(photo, dict) for photo in photos):
                    self.say(session, chat_id, "یک عکس یا «-» بفرستید.")
                    return
                photo = photos[-1]
                file_id = photo.get("file_id")
                size = photo.get("file_size")
                try:
                    if not isinstance(file_id, str) or not 1 <= len(file_id) <= 512 or (type(size) is int and size > PHOTO_LIMIT):
                        raise ValueError
                    self.api.validate_photo(file_id, PHOTO_LIMIT)
                except ValueError:
                    self.say(session, chat_id, "تصویر معتبر و حداکثر ۱ مگابایت بفرستید.")
                    return
                except BaleError as error:
                    if error.code not in (400, 403, 404):
                        raise
                    self.say(session, chat_id, "دریافت عکس انجام نشد؛ دوباره بفرستید.")
                    return
                draft["photo_id"] = file_id
            self.parent_meeting_preview(session, chat_id, state, draft)

    def parent_meeting_preview(self, session, chat_id, state, draft, delete=False):
        nonce = secrets.token_hex(8)
        self.set_state(state, mode="parent_confirm", draft=draft, delete=delete, nonce=nonce)
        self.say(session, chat_id, f"{'جلسه لغو شود؟' if delete else 'ثبت نهایی جلسه تأیید شود؟'}"
                 f"\n{draft['topic']}\n{draft['location']}\n{local_time(datetime.fromisoformat(draft['starts_at']))}"
                 f"\nتعداد کلاس: {len(draft['room_ids'])} | عکس: {'دارد' if draft.get('photo_id') else 'ندارد'}",
                 keyboard([[("تأیید", f"pm:save:{nonce}"), ("لغو", "cancel")]]))

    def parent_meeting_save(self, session, chat_id, user_id, state, pending):
        draft = pending["draft"]
        self.set_state(state)
        meeting = session.get(ParentMeeting, draft["id"]) if "id" in draft else None
        if "id" in draft and (meeting is None or meeting.cancelled or meeting.revision != draft["revision"]):
            self.say(session, chat_id, "نسخهٔ جلسه تغییر کرده؛ آن را دوباره باز کنید.")
            return
        if pending["delete"]:
            meeting.cancelled = True
            meeting.revision += 1
            links = eligible_links(meeting.id).subquery()
            guardians = session.execute(select(GuardianAccount.user_id, GuardianAccount.chat_id).where(
                GuardianAccount.user_id.in_(select(links.c.guardian_id)), GuardianAccount.chat_id.is_not(None),
                GuardianAccount.user_id.in_(select(ParentMeetingStage.guardian_id).where(
                    ParentMeetingStage.meeting_id == meeting.id, ParentMeetingStage.delivered_at.is_not(None))),
            ))
            info = detail(session, meeting)
            for guardian_id, chat in guardians:
                queue_parent_stage(session, meeting, guardian_id, chat, "cancel", info)
            action = "cancel"
        else:
            start = datetime.fromisoformat(draft["starts_at"])
            if start <= utcnow() or any(session.get(Classroom, room_id) is None for room_id in draft["room_ids"]):
                self.say(session, chat_id, "تاریخ یا کلاس‌ها دیگر معتبر نیستند.")
                return
            if meeting is None:
                meeting = ParentMeeting(section_id=draft["section_id"])
                session.add(meeting)
                action = "create"
            else:
                meeting.revision += 1
                action = "edit"
            meeting.topic, meeting.location, meeting.starts_at = draft["topic"], draft["location"], start
            meeting.photo_id = draft.get("photo_id")
            session.flush()
            existing = {item.classroom_id: item for item in session.scalars(select(ParentMeetingClass).where(
                ParentMeetingClass.meeting_id == meeting.id))}
            for room_id in existing.keys() - set(draft["room_ids"]):
                session.delete(existing[room_id])
            for room_id in set(draft["room_ids"]) - existing.keys():
                session.add(ParentMeetingClass(meeting_id=meeting.id, classroom_id=room_id))
            session.flush()
            schedule_parent_meetings(session, only=meeting.id)
        self.audit(session, user_id, f"parent_meeting_{action}", meeting_id=meeting.id, revision=meeting.revision)
        self.say(session, chat_id, "✅ تغییرات ثبت شد.", keyboard([[("مشاهده", f"pm:view:{meeting.id}")]]))

    def parent_meeting_participant(self, session, chat_id, user_id, data):
        if not self.allow(session, chat_id, ["meetings"]):
            return
        account = session.get(GuardianAccount, user_id)
        if account is None or account.chat_id != chat_id:
            self.say(session, chat_id, "برای دسترسی، دریافت پیام اولیا را با /family فعال کنید.")
            return
        _, action, *args = data.split(":")
        meetings = select(ParentMeeting).where(ParentMeeting.id.in_(linked_meetings(user_id)))
        if action == "list":
            values = identifiers(args, ID_MAX)
            if values:
                query = select(MeetingSection).where(MeetingSection.id.in_(meetings.with_only_columns(ParentMeeting.section_id)))
                self.education_page(session, chat_id, query.order_by(MeetingSection.title), values[0], "pf:list", "بخش‌های جلسات والدین",
                                    lambda section: (section.title, f"pf:section:{section.id}:0"))
        elif action == "section":
            values = identifiers(args, ID_MAX, ID_MAX)
            if values:
                self.education_page(session, chat_id, meetings.where(ParentMeeting.section_id == values[0]).order_by(
                    ParentMeeting.starts_at.desc()), values[1], f"pf:section:{values[0]}", "جلسات این بخش",
                    lambda meeting: (f"{meeting.topic[:50]} | {local_time(meeting.starts_at)}", f"pf:view:{meeting.id}"),
                    [("بازگشت به بخش‌ها", "pf:list:0")])
        elif action in ("view", "r"):
            values = identifiers(args[:2], ID_MAX, ID_MAX) if action == "r" else identifiers(args, ID_MAX)
            if values is None:
                return
            meeting = session.scalar(meetings.where(ParentMeeting.id == values[0]))
            if meeting is None:
                self.say(session, chat_id, "جلسه در دسترس نیست.")
                return
            if action == "view":
                self.say(session, chat_id, detail(session, meeting),
                         keyboard([[("بازگشت", f"pf:section:{meeting.section_id}:0")]]))
            elif len(args) == 4 and args[2] in STAGES and args[3] in ("yes", "no"):
                now = utcnow()
                stage = session.get(ParentMeetingStage, (meeting.id, values[1], user_id, args[2]))
                if stage is None or values[1] != meeting.revision or now >= utc(meeting.starts_at):
                    self.say(session, chat_id, "این دعوت منقضی شده است.")
                    return
                stage.response, stage.responded_at = args[3], now
                self.say(session, chat_id, f"✅ پاسخ «{STAGES[args[2]]}» ثبت شد: {RESPONSES[args[3]]}")
