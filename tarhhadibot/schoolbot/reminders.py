import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, func, select, update
from sqlalchemy.orm import Mapped, mapped_column

from .education_access import ID_MAX, identifiers
from .education_models import GuardianAccount, GuardianLink
from .features import enabled
from .meetings import keyboard, parse_start, utc
from .models import Base, Classroom, Enrollment, Outbox, Student, utcnow
from .parent_meeting_models import ParentMeeting, ParentMeetingClass
from .parent_meetings import detail, local_time, time_prompt
from .preferences import is_admin


AUDIENCES = {"students": "دانش‌آموزان", "parents": "اولیا"}


class SmartReminder(Base):
    __tablename__ = "smart_reminders"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    classroom_ids: Mapped[list] = mapped_column(JSON)
    audience: Mapped[str] = mapped_column(String(10))
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    meeting_id: Mapped[int | None] = mapped_column(ForeignKey("parent_meetings.id"))
    revision: Mapped[int | None] = mapped_column(Integer)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)


def reminder_targets(classrooms, audience):
    if audience == "students":
        account, chat = Student.bale_user_id, Student.chat_id
        query = select(account, chat).join(Enrollment, Enrollment.student_id == Student.id)
    else:
        account, chat = GuardianAccount.user_id, GuardianAccount.chat_id
        query = select(account, chat).join(GuardianLink).join(Student, Student.id == GuardianLink.student_id).join(
            Enrollment, Enrollment.student_id == Student.id,
        )
    return query.where(Enrollment.classroom_id.in_(classrooms), Student.active.is_(True),
                       account.is_not(None), chat.is_not(None)).distinct(), account, chat


def active_reminder_meeting(session, meeting_id, revision, now):
    meeting = session.get(ParentMeeting, meeting_id)
    if (enabled(session, "meetings") and meeting and not meeting.cancelled and meeting.revision == revision
            and utc(meeting.starts_at) > now):
        return meeting
    return None


def _meeting_classrooms(meeting_id):
    return select(ParentMeetingClass.classroom_id).where(ParentMeetingClass.meeting_id == meeting_id)


def _fingerprint(targets):
    return hashlib.sha256(repr(sorted(tuple(target) for target in targets)).encode("ascii")).hexdigest()


def valid_reminder_delivery(session, message, now, owner_id):
    _, reminder_id, account_id, _ = message.dedupe_key.split(":")
    reminder = session.get(SmartReminder, int(reminder_id))
    if (reminder is None or reminder.cancelled or not enabled(session, "reminders")
            or not is_admin(session, reminder.actor_id, owner_id)):
        return False
    if reminder.meeting_id is not None and active_reminder_meeting(
        session, reminder.meeting_id, reminder.revision, now,
    ) is None:
        return False
    query, account, chat = reminder_targets(reminder.classroom_ids, reminder.audience)
    return session.execute(query.where(account == int(account_id), chat == message.payload["chat_id"]).limit(1)).first() is not None


class SmartRemindersMixin:
    def reminder_callback(self, session, chat_id, user_id, state, data):
        if not self.allow(session, chat_id, ["reminders"]):
            return
        _, action, *args = data.split(":")
        draft = state.data
        mode = draft.get("mode")
        if action == "menu" and not args:
            self.set_state(state)
            rows = [[("✍️ پیام یا یادآوری متنی", "sr:new")]]
            if enabled(session, "meetings"):
                rows.append([("📅 یادآوری جلسهٔ والدین", "sr:meetings:0")])
            rows.append([("📋 سوابق و لغو ارسال", "sr:history:0")])
            self.say(session, chat_id, "🔔 یادآوری هوشمند", keyboard(rows))
        elif action == "new" and not args:
            self.set_state(state, mode="reminder_rooms", classrooms=[])
            self.reminder_rooms(session, chat_id, state, 0)
        elif action == "meetings" and self.allow(session, chat_id, ["meetings"]):
            page = identifiers(args, ID_MAX)
            if page is not None:
                self.set_state(state)
                self.education_page(session, chat_id, select(ParentMeeting).where(
                    ParentMeeting.cancelled.is_(False), ParentMeeting.starts_at > utcnow(),
                ).order_by(ParentMeeting.starts_at, ParentMeeting.id), page[0], "sr:meetings",
                    "جلسهٔ والدین را انتخاب کنید.",
                    lambda item: (f"{item.topic[:50]} | {local_time(item.starts_at)}", f"sr:pick:{item.id}"),
                    [("بازگشت", "sr:menu")])
        elif action == "pick" and self.allow(session, chat_id, ["meetings"]):
            values = identifiers(args, ID_MAX)
            meeting = session.get(ParentMeeting, values[0]) if values is not None else None
            if meeting and active_reminder_meeting(session, meeting.id, meeting.revision, utcnow()):
                self.set_state(state, mode="reminder_rooms", classrooms=[], meeting_id=meeting.id, revision=meeting.revision)
                self.reminder_rooms(session, chat_id, state, 0)
            else:
                self.say(session, chat_id, "جلسه در دسترس نیست.")
        elif action in ("rooms", "toggle") and mode == "reminder_rooms":
            values = identifiers(args, ID_MAX, ID_MAX) if action == "toggle" else identifiers(args, ID_MAX)
            if values is None:
                return
            if action == "toggle":
                room_id = values[0]
                if session.get(Classroom, room_id) is None or (
                    draft.get("meeting_id") is not None
                    and session.scalar(_meeting_classrooms(draft["meeting_id"]).where(
                        ParentMeetingClass.classroom_id == room_id,
                    )) is None
                ):
                    return
                classrooms = set(draft["classrooms"])
                if room_id not in classrooms and len(classrooms) >= 20:
                    self.say(session, chat_id, "حداکثر ۲۰ کلاس را انتخاب کنید.")
                    return
                classrooms.symmetric_difference_update({room_id})
                self.set_state(state, **{**draft, "classrooms": sorted(classrooms)})
            self.reminder_rooms(session, chat_id, state, values[-1])
        elif action == "next" and not args and mode == "reminder_rooms":
            if not draft["classrooms"]:
                self.say(session, chat_id, "حداقل یک کلاس انتخاب کنید.")
                return
            if draft.get("meeting_id") is not None:
                self.reminder_body_prompt(session, chat_id, state, "parents")
            else:
                self.set_state(state, **{**draft, "mode": "reminder_audience"})
                self.say(session, chat_id, "گیرندهٔ پیام را مشخص کنید.", keyboard([
                    [(label, f"sr:audience:{value}") for value, label in AUDIENCES.items()], [("لغو", "cancel")],
                ]))
        elif action == "audience" and mode == "reminder_audience" and len(args) == 1 and args[0] in AUDIENCES:
            self.reminder_body_prompt(session, chat_id, state, args[0])
        elif action == "now" and not args and mode == "reminder_time":
            self.reminder_preview(session, chat_id, state, None)
        elif action == "save" and mode == "reminder_confirm" and args == [draft.get("nonce")]:
            self.reminder_save(session, chat_id, user_id, state)
        elif action in ("history", "view", "stop"):
            values = identifiers(args, ID_MAX)
            if values is not None:
                self.reminder_history(session, chat_id, user_id, state, action, values[0])

    def reminder_rooms(self, session, chat_id, state, page):
        draft = state.data
        selected = draft["classrooms"]
        query = select(Classroom).order_by(Classroom.id)
        if draft.get("meeting_id") is not None:
            query = query.where(Classroom.id.in_(_meeting_classrooms(draft["meeting_id"])))
        self.education_page(session, chat_id, query, page, "sr:rooms", f"کلاس‌ها را انتخاب کنید | انتخاب‌شده: {len(selected)}",
                            lambda room: (f"{'✅' if room.id in selected else '⬜'} {room.title}",
                                          f"sr:toggle:{room.id}:{page}"),
                            [("ادامه", "sr:next"), ("لغو", "cancel")])

    def reminder_body_prompt(self, session, chat_id, state, audience):
        draft = state.data
        self.set_state(state, **{**draft, "audience": audience, "mode": "reminder_body"})
        self.say(session, chat_id, "متن یادآوری را بفرستید (حداکثر ۱۲۰۰ نویسه)."
                 + ("\nبرای ارسال فقط اطلاعات جلسه، «-» بفرستید." if draft.get("meeting_id") else ""))

    def reminder_message(self, session, chat_id, state, text):
        if not self.allow(session, chat_id, ["reminders"]):
            return
        draft = state.data
        if draft["mode"] == "reminder_body":
            if not 1 <= len(text) <= 1200 or any(ord(char) < 32 and char not in "\r\n\t" for char in text):
                self.say(session, chat_id, "متن معتبر بین ۱ تا ۱۲۰۰ نویسه بفرستید.")
                return
            self.set_state(state, **{**draft, "mode": "reminder_time", "body": text})
            self.say(session, chat_id, "ارسال فوری را بزنید یا زمان ارسال را وارد کنید.\n" + time_prompt(),
                     keyboard([[("ارسال فوری", "sr:now"), ("لغو", "cancel")]]))
        elif draft["mode"] == "reminder_time":
            try:
                send_at = parse_start(text)
                if send_at <= utcnow():
                    raise ValueError
            except (ValueError, OverflowError):
                self.say(session, chat_id, "زمان معتبر در آینده وارد کنید.\n" + time_prompt())
                return
            self.reminder_preview(session, chat_id, state, send_at)
        else:
            self.say(session, chat_id, "برای ادامه از دکمه‌های پیام قبلی استفاده کنید.")

    def reminder_content(self, session, chat_id, draft, send_at):
        body = "🔔 یادآوری\n\n" + draft["body"]
        if draft.get("meeting_id") is None:
            return body, None
        meeting = active_reminder_meeting(session, draft["meeting_id"], draft["revision"], utcnow())
        if meeting is None:
            self.say(session, chat_id, "جلسه تغییر کرده یا در دسترس نیست؛ دوباره انتخاب کنید.")
            return None
        if send_at is not None and send_at >= utc(meeting.starts_at):
            self.say(session, chat_id, "زمان ارسال باید قبل از شروع جلسه باشد.")
            return None
        body = "🔔 یادآوری جلسهٔ والدین\n\n" + detail(session, meeting, include_classrooms=False)
        if draft["body"] != "-":
            body += "\n\n" + draft["body"]
        return body, keyboard([[("مشاهده جلسه", f"pf:view:{meeting.id}")]])

    def reminder_preview(self, session, chat_id, state, send_at):
        draft = state.data
        content = self.reminder_content(session, chat_id, draft, send_at)
        if content is None:
            return
        targets = session.execute(reminder_targets(draft["classrooms"], draft["audience"])[0]).all()
        if not targets:
            self.say(session, chat_id, "هیچ گیرندهٔ فعال و متصلی در کلاس‌های انتخاب‌شده نیست.")
            return
        nonce = secrets.token_hex(8)
        self.set_state(state, **{**draft, "mode": "reminder_confirm", "nonce": nonce, "targets": _fingerprint(targets),
                                 "send_at": send_at.isoformat() if send_at else None})
        titles = list(session.scalars(select(Classroom.title).where(Classroom.id.in_(draft["classrooms"])).order_by(Classroom.id)))
        self.say(session, chat_id, f"کلاس‌ها: {'، '.join(titles)}\nمخاطب: {AUDIENCES[draft['audience']]} | {len(targets)} گیرنده"
                 f"\nزمان: {local_time(send_at) if send_at else 'فوری'}\n\n{content[0]}",
                 keyboard([[("تأیید نهایی ارسال", f"sr:save:{nonce}"), ("لغو", "cancel")]]))

    def reminder_save(self, session, chat_id, user_id, state):
        draft = state.data
        self.set_state(state)
        now = utcnow()
        send_at = datetime.fromisoformat(draft["send_at"]) if draft["send_at"] else now
        if send_at < now:
            self.say(session, chat_id, "زمان ارسال گذشته است؛ یادآوری را دوباره تعریف کنید.")
            return
        content = self.reminder_content(session, chat_id, draft, send_at)
        if content is None:
            return
        targets = session.execute(reminder_targets(draft["classrooms"], draft["audience"])[0]).all()
        if not targets or _fingerprint(targets) != draft["targets"]:
            self.say(session, chat_id, "فهرست گیرندگان تغییر کرده؛ یادآوری را دوباره تعریف کنید.")
            return
        reminder = SmartReminder(actor_id=user_id, classroom_ids=draft["classrooms"], audience=draft["audience"],
                                 send_at=send_at, meeting_id=draft.get("meeting_id"), revision=draft.get("revision"))
        session.add(reminder)
        session.flush()
        for account, target_chat in targets:
            payload = {"chat_id": target_chat, "text": content[0]}
            if content[1] is not None:
                payload["reply_markup"] = content[1]
            session.add(Outbox(method="sendMessage", payload=payload, available_at=send_at,
                               dedupe_key=f"sr:{reminder.id}:{account}:{target_chat}"))
        self.audit(session, user_id, "smart_reminder", reminder_id=reminder.id, classroom_ids=draft["classrooms"],
                   audience=draft["audience"], recipients=len(targets))
        self.say(session, chat_id, f"✅ یادآوری برای {len(targets)} گیرنده در صف قرار گرفت.",
                 keyboard([[("مشاهده وضعیت", f"sr:view:{reminder.id}"), ("بازگشت", "sr:menu")]]))

    def reminder_history(self, session, chat_id, user_id, state, action, value):
        self.set_state(state)
        if action == "history":
            self.education_page(session, chat_id, select(SmartReminder).order_by(SmartReminder.id.desc()),
                                value, "sr:history", "سوابق یادآوری‌ها",
                                lambda item: (f"#{item.id} | {local_time(item.send_at)} | {AUDIENCES[item.audience]}",
                                              f"sr:view:{item.id}"), [("بازگشت", "sr:menu")])
            return
        reminder = session.get(SmartReminder, value)
        if reminder is None:
            self.say(session, chat_id, "یادآوری پیدا نشد.")
            return
        scope = Outbox.dedupe_key.startswith(f"sr:{value}:")
        if action == "stop" and not reminder.cancelled:
            reminder.cancelled = True
            session.execute(update(Outbox).where(scope, Outbox.status.in_(("pending", "failed"))).values(
                status="expired", payload={},
            ))
            self.audit(session, user_id, "cancel_smart_reminder", reminder_id=value)
        counts = dict(session.execute(select(Outbox.status, func.count()).where(scope).group_by(Outbox.status)).all())
        rows = [[("لغو ارسال‌های باقی‌مانده", f"sr:stop:{value}")]] if not reminder.cancelled and (
            counts.get("pending") or counts.get("failed")
        ) else []
        self.say(session, chat_id, f"یادآوری #{value} | {local_time(reminder.send_at)}"
                 f"\nدر صف: {counts.get('pending', 0)} | ارسال‌شده: {counts.get('sent', 0)}"
                 f"\nناموفق: {counts.get('failed', 0)} | ارسال‌نشده/لغوشده: {counts.get('expired', 0)}",
                 keyboard(rows + [[("بازگشت", "sr:history:0")]]))
