import base64
import os
import unittest
from datetime import timedelta
from io import BytesIO
from unittest.mock import patch

from openpyxl import load_workbook
from sqlalchemy import create_engine, func, select

from schoolbot import excel
from schoolbot.api import BaleError
from schoolbot.bot import REFRESH_BUTTON
from schoolbot.config import Config
from schoolbot.consultation_models import Consultation, ConsultationBooking, ConsultationSlot
from schoolbot.consultations import parse_consultation_slots
from schoolbot.education_access import HOMEWORK_BUTTON, MENTOR_ADMIN, MENTOR_PANEL, PERMISSIONS
from schoolbot.education_models import (
    AbsenceFollowup, FamilyMessage, GuardianAccount, GuardianLink, Homework, HomeworkSubmission, Mentor, MentorClass,
    MentorRegistration, SessionReport,
)
from schoolbot.features import COINS_ADMIN_BUTTON, COINS_STUDENT_BUTTON, REMINDER_BUTTON
from schoolbot.parent_meeting_models import MeetingSection, ParentMeeting, ParentMeetingClass
from schoolbot.parent_meetings import local_time
from schoolbot.reminders import PHOTO_LIMIT, SmartReminder
from schoolbot.models import (
    Attendance, Base, Classroom, CoinAward, CoinCategory, Conversation, Discount, Enrollment,
    Inbox, Outbox, Setting, Student, utcnow,
)
from schoolbot.runtime import Runner
from schoolbot.service import apply_import


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.content = b""
        self.error = None
        self.photo_sizes = {}
        self.photo_checks = []

    def call(self, method, payload=None):
        if self.error:
            raise self.error
        self.calls.append((method, payload))
        return True

    def download(self, _document):
        return self.content

    def validate_photo(self, file_id, limit):
        self.photo_checks.append((file_id, limit))
        if self.photo_sizes[file_id] > limit:
            raise ValueError("Photo too large")

    def close(self):
        pass


class BotTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(os.getenv("TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:"))
        Base.metadata.drop_all(self.engine)
        self.api = FakeAPI()
        self.config = Config("test-token", "unused")
        self.runner = Runner(self.config, self.api, self.engine)
        self.runner.initialize()
        self.sessions = self.runner.sessions
        self.counter = 0
        with self.sessions.begin() as session:
            classroom = Classroom(title="ریاضی")
            session.add(classroom)
            session.flush()
            self.class_id = classroom.id
            student = Student(national_id="0012345678", full_name="دانش آموز", phone="09123456789")
            session.add(student)
            session.flush()
            self.student_id = student.id
            session.add(Enrollment(classroom_id=classroom.id, student_id=student.id))

    def tearDown(self):
        if self.runner.lock_connection is not None:
            self.runner.close()
            self.engine = create_engine(os.environ["TEST_DATABASE_URL"])
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def message(self, text="", user=25, document=None, chat_type="private", **extra):
        self.counter += 1
        message = {"message_id": self.counter, "from": {"id": user},
                   "chat": {"id": user, "type": chat_type}, "text": text, **extra}
        if document:
            message["document"] = document
        update = {"update_id": self.counter, "message": message}
        self.runner.ingest([update])
        self.runner.process_one()
        return update

    def callback(self, data, user=None):
        self.counter += 1
        user = self.config.admin_id if user is None else user
        self.runner.ingest([{"update_id": self.counter, "callback_query": {
            "id": f"2{self.counter}", "from": {"id": user}, "data": data,
            "message": {"chat": {"id": user, "type": "private"}},
        }}])
        self.runner.process_one()

    def latest_text(self):
        with self.sessions() as session:
            items = list(session.scalars(select(Outbox).where(Outbox.method == "sendMessage").order_by(Outbox.id)))
            return items[-1].payload["text"]

    def last_payload_for(self, chat_id):
        with self.sessions() as session:
            messages = session.scalars(select(Outbox).where(Outbox.method == "sendMessage").order_by(Outbox.id.desc()))
            return next(item.payload for item in messages if item.payload["chat_id"] == chat_id)

    def button_texts(self, payload):
        markup = payload["reply_markup"]
        return [button["text"] for row in markup.get("keyboard", markup.get("inline_keyboard", [])) for button in row]

    def current_nonce(self, user=None):
        with self.sessions() as session:
            return session.get(Conversation, self.config.admin_id if user is None else user).data["nonce"]

    def test_consultation_slot_inputs(self):
        now = utcnow()
        day = local_time(now + timedelta(days=2)).split()[0]
        slots = parse_consultation_slots(f"{day} ۱۷:۰۰-۱۸:۰۰ ۱۰", True, now)
        self.assertEqual(6, len(slots))
        self.assertEqual(timedelta(hours=1), slots[-1][1] - slots[0][0])
        self.assertTrue(all(end - start == timedelta(minutes=10) for start, end in slots))
        manual = parse_consultation_slots(f"{day} 18:00-18:25\n{day} 17:00-17:10", False, now)
        self.assertEqual([10, 25], [(end - start).seconds // 60 for start, end in manual])
        for text, automatic in (
            (f"{day} 17:00-18:00 14", True),
            (f"{day} 17:00-18:00 0", True),
            (f"{day} 17:00-17:20\n{day} 17:10-17:25", False),
            (f"{day} 18:00-17:00", False),
            (f"{local_time(now - timedelta(days=1)).split()[0]} 17:00-18:00", False),
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_consultation_slots(text, automatic, now)

    def test_consultation_booking_and_scoped_excel(self):
        owner = self.config.admin_id
        bit = PERMISSIONS["consultation"][0]
        with self.sessions.begin() as session:
            other = Student(national_id="0012345679", full_name="خانواده دوم", phone="09123456780")
            foreign = Student(national_id="0012345680", full_name="خانواده کلاس دیگر", phone="09123456781")
            other_room = Classroom(title="علوم")
            session.add_all([other, foreign, other_room, Mentor(user_id=71, name="مربی"),
                             Mentor(user_id=72, name="مربی کلاس دیگر")])
            session.add_all(GuardianAccount(user_id=guardian) for guardian in (50, 51, 52, 53))
            session.flush()
            session.add_all([
                Enrollment(classroom_id=self.class_id, student_id=other.id),
                Enrollment(classroom_id=other_room.id, student_id=foreign.id),
                GuardianLink(student_id=self.student_id, guardian_id=50),
                GuardianLink(student_id=self.student_id, guardian_id=51),
                GuardianLink(student_id=other.id, guardian_id=52),
                GuardianLink(student_id=foreign.id, guardian_id=53),
                MentorClass(mentor_id=71, classroom_id=self.class_id, permissions=0),
                MentorClass(mentor_id=72, classroom_id=other_room.id, permissions=bit),
            ])
        self.message("/consultation", user=owner)
        self.assertIn("کلاس را انتخاب", self.latest_text())
        self.callback(f"cs:new:{self.class_id}")
        self.message("=عنوان مشاوره", user=owner)
        self.message("گفت‌وگو درباره پیشرفت تحصیلی", user=owner)
        self.callback(f"cs:input:auto:{self.current_nonce()}")
        day = local_time(utcnow() + timedelta(days=2)).split()[0]
        self.message(f"{day} 17:00-18:00 10", user=owner)
        save = f"cs:save:{self.current_nonce()}"
        self.callback(save)
        self.callback(save)
        with self.sessions() as session:
            consultation_id = session.scalars(select(Consultation.id)).one()
            slots = list(session.scalars(select(ConsultationSlot.id).order_by(ConsultationSlot.starts_at)))
            self.assertEqual(6, len(slots))
        for guardian in (50, 51, 52, 53):
            self.message("/family", user=guardian)
        self.callback(f"cf:slots:{consultation_id}:0", user=53)
        self.assertIn("در دسترس نیست", self.latest_text())
        for guardian in (50, 52):
            self.callback(f"cf:pick:{consultation_id}:{slots[0]}", user=guardian)
        self.callback(f"cf:book:{consultation_id}:{slots[0]}:{self.current_nonce(50)}", user=50)
        self.assertIn("ثبت شد", self.latest_text())
        self.callback(f"cf:book:{consultation_id}:{slots[0]}:{self.current_nonce(52)}", user=52)
        self.assertIn("دیگر آزاد نیست", self.latest_text())
        self.callback(f"cf:slots:{consultation_id}:0", user=52)
        self.assertFalse(any("17:00" in label for label in self.button_texts(self.last_payload_for(52))))
        self.callback(f"cf:pick:{consultation_id}:{slots[1]}", user=51)
        self.assertIn("نوبت ثبت‌شده خانواده", self.latest_text())
        with self.sessions() as session:
            self.assertEqual(50, session.scalars(select(ConsultationBooking.guardian_id)).one())

        export = f"cs:export:{consultation_id}"
        self.callback(export, user=71)
        self.assertIn("مجاز نیست", self.latest_text())
        self.callback(f"ed:perm:71:{self.class_id}:{list(PERMISSIONS).index('consultation')}:1")
        self.callback(export, user=72)
        self.assertIn("مجاز نیست", self.latest_text())
        self.callback(export, user=71)
        self.callback(export)
        with self.sessions() as session:
            files = list(session.scalars(select(Outbox).where(Outbox.method == "sendDocument").order_by(Outbox.id)))
            self.assertEqual([71, owner], [item.payload["chat_id"] for item in files])
            book = load_workbook(BytesIO(base64.b64decode(files[0].payload["_file"]["content"])))
            self.assertEqual("s", book.active["A2"].data_type)
            self.assertEqual("=عنوان مشاوره", book.active["A2"].value)
            self.assertEqual(("رزرو شده", "دانش آموز", "50"), tuple(cell.value for cell in book.active[2][3:6]))
            self.assertEqual("آزاد", book.active["D3"].value)
        self.callback(f"ed:perm:71:{self.class_id}:{list(PERMISSIONS).index('consultation')}:0")
        while self.runner.deliver_one():
            pass
        self.assertEqual([owner], [payload["chat_id"] for method, payload in self.api.calls if method == "sendDocument"])

    def add_admin(self, user=26):
        self.callback("admin_add")
        self.message(str(user), user=self.config.admin_id)
        self.callback("confirm:" + self.current_nonce())

    def create_record(self, code, description, user=None):
        user = self.config.admin_id if user is None else user
        self.callback(f"record:{self.class_id}", user=user)
        self.message("0012345678", user=user)
        self.message(code, user=user)
        self.message(description, user=user)
        nonce = self.current_nonce(user)
        self.callback("confirm:" + nonce, user=user)
        return nonce

    def pending(self, kind="attendance", status="حاضر"):
        row = {"national_id": "0012345678", "name": "دانش آموز"}
        if kind == "attendance":
            row["status"] = status
        elif kind == "students":
            row["phone"] = "09123456789"
        else:
            row.update(code="OFF-20", description="دوره اول")
        return {"kind": kind, "classroom_id": self.class_id,
                "session_date": excel.today()[0].isoformat(), "rows": [row]}

    def confirm_education(self, user=None, prefix="ed"):
        action = f"{prefix}:confirm:{self.current_nonce(user)}"
        self.callback(action, user=user)
        return action

    def educational_fixture(self, mentor=41, permissions=()):
        with self.sessions.begin() as session:
            other = Classroom(title="علوم")
            session.add_all([other, Mentor(user_id=mentor, name="مربی")])
            session.flush()
            session.add(MentorClass(mentor_id=mentor, classroom_id=self.class_id,
                                    permissions=sum(PERMISSIONS[key][0] for key in permissions)))
            return other.id

    def test_smart_reminder_multiple_classes_deduplicates_and_requires_admin(self):
        owner = self.config.admin_id
        with self.sessions.begin() as session:
            other = Classroom(title="علوم")
            third = Classroom(title="تاریخ")
            extra = Student(national_id="0000000000", full_name="عضو علوم", phone="", bale_user_id=26, chat_id=26)
            excluded = Student(national_id="0000000001", full_name="عضو تاریخ", phone="", bale_user_id=27, chat_id=27)
            session.add_all([other, third, extra, excluded])
            session.flush()
            second_id = other.id
            session.add_all([Enrollment(classroom_id=other.id, student_id=self.student_id),
                             Enrollment(classroom_id=other.id, student_id=extra.id),
                             Enrollment(classroom_id=third.id, student_id=excluded.id)])
        self.message("0012345678")
        self.callback("sr:new", user=25)
        self.assertIn("مجاز نیست", self.latest_text())
        self.message(REMINDER_BUTTON, user=owner)
        self.assertIn("پیام یا یادآوری متنی", " ".join(self.button_texts(self.last_payload_for(owner))))
        self.callback("sr:new")
        for classroom_id in (self.class_id, second_id):
            self.callback(f"sr:toggle:{classroom_id}:0")
        self.callback("sr:next")
        self.callback("sr:audience:students")
        self.message("فردا کتاب بخوانید.", user=owner)
        self.callback("sr:now")
        self.assertIn("2 گیرنده", self.latest_text())
        confirm = "sr:save:" + self.current_nonce()
        self.callback(confirm)
        self.callback(confirm)
        while self.runner.deliver_one():
            pass
        self.assertEqual(sorted(payload["chat_id"] for _, payload in self.api.calls
                                if payload.get("text", "").startswith("🔔 یادآوری\n")), [25, 26])
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(SmartReminder)), 1)

    def test_smart_reminder_photo_size_caption_and_scheduled_delivery(self):
        owner = self.config.admin_id
        self.message("0012345678")
        self.callback("sr:new")
        self.callback(f"sr:toggle:{self.class_id}:0")
        self.callback("sr:next")
        self.callback("sr:audience:students")
        self.message(user=owner, photo=[{"file_id": "declared-large", "file_size": PHOTO_LIMIT + 1}])
        self.assertIn("حداکثر ۲ مگابایت", self.latest_text())
        self.assertEqual(self.api.photo_checks, [])
        self.api.photo_sizes["actual-large"] = PHOTO_LIMIT + 1
        self.message(user=owner, photo=[{"file_id": "actual-large", "file_size": 100}])
        self.assertIn("حداکثر ۲ مگابایت", self.latest_text())
        self.api.photo_sizes["accepted"] = PHOTO_LIMIT
        self.message(user=owner, photo=[{"file_id": "accepted"}], caption="پوستر برنامه")
        self.assertEqual(self.api.photo_checks, [("actual-large", PHOTO_LIMIT), ("accepted", PHOTO_LIMIT)])
        self.message(local_time(utcnow() + timedelta(days=1)), user=owner)
        self.assertIn("پیوست: عکس", self.latest_text())
        self.callback("sr:save:" + self.current_nonce())
        while self.runner.deliver_one():
            pass
        self.assertFalse(any(method == "sendPhoto" for method, _ in self.api.calls))
        with self.sessions.begin() as session:
            item = session.scalars(select(Outbox).where(Outbox.dedupe_key.startswith("sr:"))).one()
            self.assertEqual(item.method, "sendPhoto")
            item.available_at = utcnow() - timedelta(seconds=1)
        self.assertTrue(self.runner.deliver_one())
        self.assertIn(("sendPhoto", {"chat_id": 25, "photo": "accepted", "caption": "🔔 یادآوری\n\nپوستر برنامه"}), self.api.calls)

    def test_smart_reminder_scheduled_cancellation(self):
        owner = self.config.admin_id
        self.message("0012345678")
        self.callback("sr:new")
        self.callback(f"sr:toggle:{self.class_id}:0")
        self.callback("sr:next")
        self.callback("sr:audience:students")
        self.message("آزمون", user=owner)
        self.message(local_time(utcnow() + timedelta(days=1)), user=owner)
        self.callback("sr:save:" + self.current_nonce())
        while self.runner.deliver_one():
            pass
        with self.sessions() as session:
            pending = session.scalars(select(Outbox).where(Outbox.dedupe_key.startswith("sr:"))).one()
            self.assertEqual(pending.status, "pending")
            identifier = session.scalar(select(SmartReminder.id))
        self.callback(f"sr:stop:{identifier}")
        with self.sessions() as session:
            pending = session.scalars(select(Outbox).where(Outbox.dedupe_key.startswith("sr:"))).one()
            self.assertEqual((pending.status, pending.payload), ("expired", {}))

    def test_smart_reminder_meeting_targets_only_selected_classes_and_expires_after_edit(self):
        from schoolbot.education_models import GuardianAccount

        owner = self.config.admin_id
        with self.sessions.begin() as session:
            section = MeetingSection(title="عمومی")
            other = Classroom(title="علوم")
            outsider = Student(national_id="0000000000", full_name="عضو علوم", phone="")
            session.add_all([section, other, outsider, GuardianAccount(user_id=55, chat_id=55),
                             GuardianAccount(user_id=56, chat_id=56)])
            session.flush()
            meeting = ParentMeeting(section_id=section.id, topic="معارفه", location="سالن",
                                    starts_at=utcnow() + timedelta(days=3))
            session.add(meeting)
            session.flush()
            meeting_id = meeting.id
            session.add_all([Enrollment(classroom_id=other.id, student_id=outsider.id),
                             GuardianLink(student_id=outsider.id, guardian_id=56),
                             GuardianLink(student_id=self.student_id, guardian_id=55),
                             ParentMeetingClass(meeting_id=meeting.id, classroom_id=self.class_id),
                             ParentMeetingClass(meeting_id=meeting.id, classroom_id=other.id)])
        self.callback(f"sr:pick:{meeting_id}")
        self.callback(f"sr:toggle:{self.class_id}:0")
        self.callback("sr:next")
        self.api.photo_sizes["meeting-poster"] = 100
        self.message(user=owner, photo=[{"file_id": "meeting-poster"}])
        self.callback("sr:now")
        self.callback("sr:save:" + self.current_nonce())
        with self.sessions.begin() as session:
            notice = session.scalars(select(Outbox).where(Outbox.dedupe_key.startswith("sr:"))).one()
            self.assertEqual(notice.method, "sendPhoto")
            self.assertIn("معارفه", notice.payload["caption"])
            self.assertEqual(notice.payload["chat_id"], 55)
            self.assertEqual(notice.payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"], f"pf:view:{meeting_id}")
            session.get(ParentMeeting, meeting_id).revision += 1
        while self.runner.deliver_one():
            pass
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(Outbox.status).where(Outbox.dedupe_key.startswith("sr:"))), "expired")

    def test_mentor_excel_registration_requires_single_use_activation(self):
        owner = self.config.admin_id
        self.callback("ed:uploadmentor")
        self.api.content = excel.workbook("mentors", [("مربی نمونه", "۰۰۱۲۳۴۵۶۷۸", "۰۹۱۲۳۴۵۶۷۸۹")])
        self.message(user=owner, document={"file_id": "fixture"})
        self.callback("mi:" + self.current_nonce())
        with self.sessions() as session:
            output = session.scalar(select(Outbox).where(Outbox.method == "sendDocument").order_by(Outbox.id.desc()).limit(1))
            registration = session.get(MentorRegistration, "0012345678")
            self.assertIsNone(registration.user_id)
            book = load_workbook(BytesIO(base64.b64decode(output.payload["_file"]["content"])))
        code = book.active["D2"].value
        book.close()
        self.message("/mentor", user=41)
        self.message("0012345678 09123456789", user=41)
        self.assertIn("معتبر نیست", self.latest_text())
        self.message(f"0012345678 09123456789 {code}", user=41)
        self.assertIn("خوش آمدید", self.latest_text())
        self.message("/mentor", user=42)
        self.message(f"0012345678 09123456789 {code}", user=42)
        self.assertIn("معتبر نیست", self.latest_text())
        with self.sessions() as session:
            registration = session.get(MentorRegistration, "0012345678")
            self.assertEqual((registration.user_id, registration.activation_hash), (41, None))
            self.assertIsNone(session.get(Mentor, 42))

    def test_family_phones_and_mentor_coin_permissions_are_class_scoped(self):
        other = self.educational_fixture(permissions=("family", "coins"))
        data = excel.workbook("students", [("دانش آموز", "0012345678", "09123456789", "۰۹۱۱۱۱۱۱۱۱۱", "۰۹۲۲۲۲۲۲۲۲۲")],
                              self.class_id)
        with self.sessions.begin() as session:
            apply_import(session, excel.parse(data, "students", self.class_id))
            apply_import(session, self.pending(kind="students"))
        self.callback(f"ed:familycontact:{self.class_id}:{self.student_id}", user=41)
        self.assertIn("09111111111", self.latest_text())
        self.assertIn("09222222222", self.latest_text())
        self.callback(f"ed:familycontact:{other}:{self.student_id}", user=41)
        self.assertIn("مجاز نیست", self.latest_text())
        category_id = self.coin_category()
        self.callback(f"coins:preview:{self.class_id}:{category_id}:{self.student_id}", user=41)
        stale = "coins:confirm:" + self.current_nonce(41)
        self.callback(f"ed:perm:41:{self.class_id}:5:0")
        self.callback(stale, user=41)
        self.assertIn("مجوز ثبت سکه", self.latest_text())
        self.callback(f"ed:perm:41:{self.class_id}:5:1")
        self.callback(f"coins:preview:{self.class_id}:{category_id}:{self.student_id}", user=41)
        self.confirm_education(41, prefix="coins")
        self.callback(f"coins:report:{self.class_id}", user=41)
        self.assertIn("مجاز نیست", self.latest_text())
        with self.sessions() as session:
            award = session.scalars(select(CoinAward)).one()
            self.assertEqual((award.actor_id, award.amount), (41, 15))

    def test_education_mentor_registration_multiple_classes_and_restricted_permissions(self):
        owner = self.config.admin_id
        with self.sessions.begin() as session:
            second_class = Classroom(title="علوم")
            session.add(second_class)
            session.flush()
            second = second_class.id
        self.message(MENTOR_ADMIN, user=owner)
        self.callback("ed:add")
        self.message("مربی نمونه\n۴۱", user=owner)
        self.confirm_education()
        self.message("/start", user=41)
        self.assertIn(MENTOR_PANEL, self.button_texts(self.last_payload_for(41)))
        self.message(MENTOR_PANEL, user=41)
        self.assertIn("موردی برای نمایش نیست", self.latest_text())
        for class_id in (self.class_id, second):
            self.callback(f"ed:mclass:41:{class_id}")
            self.confirm_education()
        self.callback(f"ed:roster:{self.class_id}:0", user=41)
        self.assertIn("مجاز نیست", self.latest_text())
        self.callback(f"ed:perm:41:{self.class_id}:0:1")
        self.callback(f"ed:roster:{self.class_id}:0", user=41)
        self.assertIn("دانش آموز", self.button_texts(self.last_payload_for(41)))
        self.callback(f"ed:roster:{second}:0", user=41)
        self.assertIn("مجاز نیست", self.latest_text())
        self.callback(f"ed:perm:41:{second}:0:1", user=41)
        self.assertIn("فقط ادمین", self.latest_text())
        self.callback("coins:menu", user=41)
        self.assertIn("دسترسی مجاز نیست", self.latest_text())
        self.callback("ed:active:41:0")
        self.callback(f"ed:class:{self.class_id}", user=41)
        self.assertIn("پنل آموزشی مجاز نیست", self.latest_text())
        while self.runner.deliver_one():
            pass
        self.assertFalse(any(payload.get("chat_id") == 41 and "فهرست اعضای فعال" in payload.get("text", "")
                             for _, payload in self.api.calls))

    def test_education_report_scope_expiry_and_replayed_confirmation(self):
        other = self.educational_fixture(permissions=("reports",))
        self.callback(f"ed:reportnew:{other}", user=41)
        self.assertIn("مجاز نیست", self.latest_text())
        with self.sessions.begin() as session:
            session.add(SessionReport(classroom_id=other, actor_id=self.config.admin_id,
                                      session_date=excel.today()[0], topic="خصوصی", presence="حاضر",
                                      activity="تمرین", next_action="پیگیری"))
            session.flush()
            other_report = session.scalar(select(SessionReport.id))
        self.callback(f"ed:report:{self.class_id}:{other_report}", user=41)
        self.assertIn("پیدا نشد", self.latest_text())
        for attempt in range(2):
            self.callback(f"ed:reportnew:{self.class_id}", user=41)
            self.message("درس اول\nهمه حاضر\nتمرین\nمرور", user=41)
            if attempt == 0:
                self.callback(f"ed:perm:41:{self.class_id}:1:0")
                self.confirm_education(41)
                self.assertIn("مجاز نیست", self.latest_text())
                self.callback(f"ed:perm:41:{self.class_id}:1:1")
            else:
                nonce = self.confirm_education(41)
                self.callback(nonce, user=41)
        with self.sessions() as session:
            report = session.scalars(select(SessionReport).where(SessionReport.classroom_id == self.class_id)).one()
            self.assertEqual((report.actor_id, report.topic, report.presence), (41, "درس اول", "همه حاضر"))

    def test_education_bulk_attendance_and_admin_referral_queue(self):
        other = self.educational_fixture(permissions=("attendance",))
        self.message("0012345678")
        self.callback(f"ed:roll:{other}", user=41)
        self.assertIn("مجاز نیست", self.latest_text())
        self.callback(f"ed:roll:{self.class_id}", user=41)
        nonce = self.current_nonce(41)
        self.callback(f"ed:rollall:{nonce}:0", user=41)
        self.callback(f"ed:rollmark:{nonce}:{self.student_id}:0", user=41)
        self.callback(f"ed:rollreview:{nonce}", user=41)
        self.confirm_education(41)
        with self.sessions() as session:
            attendance = session.scalars(select(Attendance)).one()
            self.assertEqual(attendance.status, "غایب")
            attendance_id = attendance.id
        self.assertIn("غایب", self.last_payload_for(25)["text"])
        self.callback(f"ed:followup:{self.class_id}:{attendance_id}", user=41)
        self.message("نامشخص\nارجاع", user=41)
        self.confirm_education(41)
        self.callback(f"ed:resolve:{self.class_id}:{attendance_id}", user=41)
        self.assertIn("فقط برای ادمین", self.latest_text())
        self.callback("ed:referralqueue:0")
        self.assertTrue(any("ریاضی" in label for label in self.button_texts(self.last_payload_for(self.config.admin_id))))
        self.callback(f"ed:resolve:{self.class_id}:{attendance_id}")
        self.confirm_education()
        with self.sessions() as session:
            self.assertTrue(session.get(AbsenceFollowup, attendance_id).resolved)

    def test_education_homework_submission_private_feedback_and_membership(self):
        other = self.educational_fixture(permissions=("homework",))
        self.message("0012345678")
        with self.sessions.begin() as session:
            student = Student(national_id="0000000000", full_name="دانش‌آموز دیگر", phone="", bale_user_id=26, chat_id=26)
            session.add(student)
            session.flush()
            session.add(Enrollment(classroom_id=other, student_id=student.id))
        self.callback(f"ed:hwnew:{self.class_id}", user=41)
        self.message(f"تمرین\n{excel.today()[1]}\nپاسخ در یک پیام", user=41)
        self.confirm_education(41)
        with self.sessions() as session:
            homework_id = session.scalar(select(Homework.id))
        self.message(HOMEWORK_BUTTON)
        self.assertIn("ریاضی | تمرین", self.button_texts(self.last_payload_for(25)))
        self.callback(f"hw:open:{homework_id}", user=26)
        self.assertIn("در کلاس‌های شما نیست", self.latest_text())
        self.callback(f"hw:submit:{homework_id}", user=25)
        self.message("پاسخ خصوصی دانش‌آموز")
        self.confirm_education(25, prefix="hw")
        self.callback(f"ed:submissions:{self.class_id}:{homework_id}:0", user=41)
        self.assertIn("دانش آموز", self.button_texts(self.last_payload_for(41)))
        self.callback(f"ed:submission:{self.class_id}:{homework_id}:{self.student_id}", user=41)
        self.assertIn("پاسخ خصوصی دانش‌آموز", self.latest_text())
        self.callback(f"ed:feedback:{self.class_id}:{homework_id}:{self.student_id}", user=41)
        self.message("عالی بود", user=41)
        self.confirm_education(41)
        self.assertIn("بازخورد ثبت شد", self.last_payload_for(25)["text"])
        self.callback(f"hw:open:{homework_id}", user=25)
        self.assertIn("عالی بود", self.latest_text())
        self.callback(f"hw:submit:{homework_id}", user=25)
        self.assertIn("ارزیابی شده", self.latest_text())
        with self.sessions() as session:
            self.assertEqual(session.get(HomeworkSubmission, (homework_id, self.student_id)).reviewer_id, 41)

    def test_education_family_requires_link_and_opt_in_and_rechecks_at_delivery(self):
        other = self.educational_fixture(permissions=("family",))
        owner = self.config.admin_id
        with self.sessions.begin() as session:
            sibling = Student(national_id="0000000000", full_name="همکلاسی", phone="")
            outside = Student(national_id="0000000001", full_name="کلاس دیگر", phone="")
            session.add_all([sibling, outside])
            session.flush()
            sibling_id, outside_id = sibling.id, outside.id
            session.add_all([Enrollment(classroom_id=self.class_id, student_id=sibling_id),
                             Enrollment(classroom_id=other, student_id=outside_id)])
        for class_id, student_id, guardian_id in ((self.class_id, self.student_id, 50),
                                                   (self.class_id, sibling_id, 50),
                                                   (other, outside_id, 51), (self.class_id, sibling_id, 52)):
            self.callback(f"ed:glink:{class_id}:{student_id}")
            self.message(str(guardian_id), user=owner)
            self.confirm_education()
        self.message("/family", user=53)
        self.assertIn("ثبت نشده", self.latest_text())
        self.message("/family", user=50)
        self.message("/family", user=51)
        self.callback(f"ed:familymsg:{self.class_id}:0", user=41)
        self.message("پیام گروهی کلاس", user=41)
        self.assertIn("ارسال به 1 حساب", self.latest_text())
        self.confirm_education(41)
        while self.runner.deliver_one():
            pass
        self.assertEqual([payload["chat_id"] for _, payload in self.api.calls
                          if payload.get("text", "").startswith("✉️ پیام آموزشی")], [50])
        self.callback(f"ed:familymsg:{self.class_id}:{self.student_id}", user=41)
        self.message("پیام فردی کلاس", user=41)
        self.confirm_education(41)
        self.message("/family_off", user=50)
        while self.runner.deliver_one():
            pass
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(GuardianLink)), 4)
            self.assertEqual(session.scalar(select(func.count()).select_from(FamilyMessage)), 2)
            messages = list(session.scalars(select(Outbox).where(Outbox.dedupe_key.startswith("edfamily:"))))
            self.assertEqual([(item.dedupe_key.rsplit(":", 1)[-1], item.status) for item in messages],
                             [("50", "sent"), ("50", "expired")])

    def coin_category(self, amount="۱۵"):
        self.callback("coins:new")
        self.message("فعالیت كلاسی", user=self.config.admin_id)
        self.message(amount, user=self.config.admin_id)
        with self.sessions() as session:
            return session.scalar(select(CoinCategory.id))

    def coin_preview(self, category_id):
        self.callback(f"coins:preview:{self.class_id}:{category_id}:{self.student_id}")
        return "coins:confirm:" + self.current_nonce()

    def test_coins_navigation_award_balance_and_notification(self):
        self.message("0012345678")
        self.assertIn(COINS_STUDENT_BUTTON, self.button_texts(self.last_payload_for(25)))
        self.message(COINS_STUDENT_BUTTON)
        self.assertIn("شما: 0", self.latest_text())
        self.message(COINS_ADMIN_BUTTON, user=self.config.admin_id)

        def click(label):
            rows = self.last_payload_for(self.config.admin_id)["reply_markup"]["inline_keyboard"]
            self.callback(next(button["callback_data"] for row in rows for button in row if label in button["text"]))

        click("ایجاد بخش امتیاز")
        self.message("فعالیت كلاسی", user=self.config.admin_id)
        self.message("۰", user=self.config.admin_id)
        with self.sessions() as session:
            self.assertIsNone(session.scalar(select(CoinCategory.id)))
        self.message("۱۵", user=self.config.admin_id)
        click("مدیریت امتیازات")
        click("ثبت امتیاز و گزارش کلاس‌ها")
        click("ریاضی")
        click("ثبت امتیاز")
        click("فعالیت کلاسی")
        click("0012345678")
        confirm = "coins:confirm:" + self.current_nonce()
        click("تأیید ثبت")
        self.callback(confirm)
        notice = self.last_payload_for(25)
        self.assertIn("برای شما 15 سکه", notice["text"])
        self.message(COINS_STUDENT_BUTTON)
        self.assertIn("شما: 15", self.latest_text())
        self.assertIn("فعالیت کلاسی | ریاضی", self.latest_text())
        with self.sessions() as session:
            award = session.scalars(select(CoinAward)).one()
            self.assertEqual((award.amount, award.actor_id), (15, self.config.admin_id))
            self.assertEqual(session.scalar(select(func.count()).select_from(Outbox).where(
                Outbox.dedupe_key.startswith("coins:"),
            )), 1)
        while self.runner.deliver_one():
            pass
        self.assertIn(("sendMessage", notice), self.api.calls)

    def test_coins_recheck_amount_membership_and_feature_and_keep_history(self):
        category_id = self.coin_category()
        self.add_admin()
        self.callback(self.coin_preview(category_id))
        stale = self.coin_preview(category_id)
        self.callback(f"coins:edit:{category_id}", user=26)
        self.message("۳۰", user=26)
        self.callback(stale)
        self.assertIn("تغییر کرده", self.latest_text())
        self.callback(self.coin_preview(category_id))
        blocked = self.coin_preview(category_id)
        self.callback("feature:coins:0", user=26)
        self.callback(blocked)
        self.assertIn("غیرفعال", self.latest_text())
        self.callback("feature:coins:1", user=26)
        invalid = self.coin_preview(category_id)
        with self.sessions.begin() as session:
            session.delete(session.get(Enrollment, (self.class_id, self.student_id)))
        self.callback(invalid)
        self.assertIn("عضو فعال", self.latest_text())
        with self.sessions() as session:
            self.assertEqual(list(session.scalars(select(CoinAward.amount).order_by(CoinAward.id))), [15, 30])
            self.assertIsNone(session.scalar(select(Outbox.id).where(Outbox.dedupe_key.startswith("coins:"))))

    def test_coins_report_is_class_scoped_and_safe_for_excel(self):
        category_id = self.coin_category()
        self.callback(self.coin_preview(category_id))
        with self.sessions.begin() as session:
            session.get(Student, self.student_id).full_name = "=1+1"
            session.delete(session.get(Enrollment, (self.class_id, self.student_id)))
            other = Classroom(title="کلاس دیگر")
            zero = Student(national_id="0000000000", full_name="عضو بدون سکه", phone="")
            session.add_all([other, zero])
            session.flush()
            session.add(Enrollment(classroom_id=self.class_id, student_id=zero.id))
            session.add(CoinAward(classroom_id=other.id, student_id=self.student_id, category_id=category_id,
                                  category_title="فعالیت کلاسی", amount=60, actor_id=self.config.admin_id))
        self.callback(f"coins:report:{self.class_id}", user=25)
        self.assertIn("دسترسی مجاز نیست", self.latest_text())
        with self.sessions() as session:
            self.assertIsNone(session.scalar(select(Outbox.id).where(Outbox.method == "sendDocument")))
        self.callback(f"coins:report:{self.class_id}")
        with self.sessions() as session:
            file = session.scalars(select(Outbox).where(Outbox.method == "sendDocument")).one().payload["_file"]
        book = load_workbook(BytesIO(base64.b64decode(file["content"])))
        summary = book["خلاصه سکه‌ها"]
        rows = {row[0].value: row for row in summary.iter_rows(min_row=2)}
        self.assertEqual(rows["0012345678"][1].data_type, "s")
        self.assertEqual([cell.value for cell in rows["0012345678"]], ["0012345678", "=1+1", "عضو سابق", 15])
        self.assertEqual([cell.value for cell in rows["0000000000"]], ["0000000000", "عضو بدون سکه", "فعال", 0])
        self.assertEqual(book["ریز امتیازات"].max_row, 2)
        self.assertEqual(book["ریز امتیازات"]["D2"].value, 15)

    def test_coin_notices_do_not_reach_an_unlinked_account(self):
        self.message("0012345678")
        self.callback(self.coin_preview(self.coin_category()))
        with self.sessions.begin() as session:
            student = session.get(Student, self.student_id)
            student.bale_user_id = None
            student.chat_id = None
        while self.runner.deliver_one():
            pass
        with self.sessions() as session:
            notice = session.scalars(select(Outbox).where(Outbox.dedupe_key.startswith("coins:"))).one()
            self.assertEqual((notice.status, notice.payload), ("expired", {}))
        self.assertFalse(any("برای شما 15 سکه" in payload.get("text", "") for _, payload in self.api.calls))

    def test_login_persian_digits(self):
        self.message("۰۰۱۲۳۴۵۶۷۸")
        with self.sessions() as session:
            student = session.get(Student, self.student_id)
            self.assertEqual(student.chat_id, 25)
        self.assertIn("خوش آمدید", self.latest_text())

    def test_feature_settings_are_admin_only_and_persistent(self):
        self.callback("feature:discounts:0", user=25)
        with self.sessions() as session:
            self.assertIsNone(session.get(Setting, "feature:discounts"))
        self.callback("feature:discounts:0")
        with self.sessions() as session:
            self.assertEqual(session.get(Setting, "feature:discounts").value, "0")
        self.message("0012345678")
        with self.sessions() as session:
            item = session.scalars(select(Outbox).where(Outbox.method == "sendMessage").order_by(Outbox.id.desc())).first()
            self.assertNotIn("🎁 تخفیف‌های من", self.button_texts(item.payload))
        self.message("🎁 تخفیف‌های من")
        self.assertIn("غیرفعال", self.latest_text())
        self.callback("feature:discounts:1")
        self.message("🎁 تخفیف‌های من")
        self.assertIn("تخفیفی برای شما ثبت نشده", self.latest_text())

    def test_disabled_features_block_old_class_buttons_and_hide_menu(self):
        self.callback("feature:attendance:0")
        self.callback(f"attend:{self.class_id}")
        self.assertIn("غیرفعال", self.latest_text())
        self.callback(f"upload:attendance:{self.class_id}")
        self.assertIn("غیرفعال", self.latest_text())
        self.callback(f"class:{self.class_id}")
        with self.sessions() as session:
            item = session.scalars(select(Outbox).where(Outbox.method == "sendMessage").order_by(Outbox.id.desc())).first()
            self.assertNotIn("attendance", str(item.payload["reply_markup"]))
            self.assertNotIn("attend:", str(item.payload["reply_markup"]))

    def test_excel_can_be_disabled_without_disabling_manual_attendance(self):
        self.callback("feature:excel:0")
        self.callback(f"export:attendance:{self.class_id}")
        self.assertIn("غیرفعال", self.latest_text())
        self.callback(f"attend:{self.class_id}")
        with self.sessions() as session:
            nonce = session.get(Conversation, self.config.admin_id).data["nonce"]
        self.callback(f"mark:{nonce}:{self.student_id}:present")
        self.callback(f"confirm:{nonce}")
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Attendance)), 1)

    def test_disabled_feature_blocks_pending_confirmation(self):
        self.callback(f"attend:{self.class_id}")
        with self.sessions() as session:
            nonce = session.get(Conversation, self.config.admin_id).data["nonce"]
        self.callback(f"mark:{nonce}:{self.student_id}:present")
        with self.sessions.begin() as session:
            session.add(Setting(key="feature:manual_attendance", value="0"))
        self.callback(f"confirm:{nonce}")
        self.assertIn("غیرفعال", self.latest_text())
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Attendance)), 0)

    def test_owner_adds_admin_with_confirmation_and_persian_id(self):
        self.message("👥 مدیریت ادمین‌ها", user=self.config.admin_id)
        self.callback("admin_add")
        self.message("۲۶", user=self.config.admin_id)
        with self.sessions() as session:
            self.assertIsNone(session.get(Setting, "admin:26"))
        nonce = self.current_nonce()
        self.callback("confirm:" + nonce)
        with self.sessions() as session:
            self.assertEqual(session.get(Setting, "admin:26").value, "1")
        self.message("/start", user=26)
        payload = self.last_payload_for(26)
        self.assertIn("🏫 کلاس‌ها", self.button_texts(payload))
        self.assertNotIn("👥 مدیریت ادمین‌ها", self.button_texts(payload))
        self.message("➕ ایجاد کلاس", user=26)
        self.message("کلاس ادمین دوم", user=26)
        with self.sessions() as session:
            self.assertIsNotNone(session.scalar(select(Classroom).where(Classroom.title == "کلاس ادمین دوم")))
            self.assertEqual(session.get(Setting, "admin_chat_id").value, str(self.config.admin_id))

    def test_added_admin_cannot_manage_admins(self):
        self.add_admin()
        for data in ("admin_add", "admins:0", f"admin_remove:{self.config.admin_id}", "admin_remove:26"):
            self.callback(data, user=26)
            self.assertIn("فقط مدیر اصلی", self.latest_text())
        self.message("👥 مدیریت ادمین‌ها", user=26)
        self.assertIn("فقط مدیر اصلی", self.latest_text())
        with self.sessions.begin() as session:
            state = session.get(Conversation, 26)
            self.runner.bot.set_state(state, mode="confirm_admin", operation="add", admin_id=27, nonce="forged")
        self.callback("confirm:forged", user=26)
        with self.sessions() as session:
            self.assertIsNone(session.get(Setting, "admin:27"))

    def test_student_cannot_add_admin(self):
        self.callback("admin_add", user=25)
        self.assertIn("دسترسی مجاز نیست", self.latest_text())
        with self.sessions() as session:
            self.assertIsNone(session.get(Setting, "admin:25"))

    def test_remove_admin_revokes_pending_operations_immediately(self):
        self.add_admin()
        self.callback(f"attend:{self.class_id}", user=26)
        attendance_nonce = self.current_nonce(26)
        self.callback(f"mark:{attendance_nonce}:{self.student_id}:present", user=26)
        self.callback("admin_remove:26")
        with self.sessions() as session:
            self.assertIsNotNone(session.get(Setting, "admin:26"))
        self.callback("confirm:" + self.current_nonce())
        with self.sessions() as session:
            self.assertIsNone(session.get(Setting, "admin:26"))
            self.assertEqual(session.get(Conversation, 26).data, {})
        self.assertEqual(self.last_payload_for(26)["reply_markup"], {"remove_keyboard": True})
        self.callback("confirm:" + attendance_nonce, user=26)
        self.assertIn("دسترسی مجاز نیست", self.latest_text())
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Attendance)), 0)

    def test_owner_cannot_be_removed_and_admin_ids_are_validated(self):
        self.callback(f"admin_remove:{self.config.admin_id}")
        self.assertIn("قابل حذف نیست", self.latest_text())
        self.callback("admin_add")
        for identifier in ("@someone", "0", "-2", "9223372036854775808", "²"):
            self.message(identifier, user=self.config.admin_id)
            self.assertIn("شناسه عددی مثبت", self.latest_text())
        self.message(str(self.config.admin_id), user=self.config.admin_id)
        self.assertIn("از قبل ادمین", self.latest_text())

    def test_cancel_admin_add_and_replay_confirmation(self):
        self.callback("admin_add")
        self.message("26", user=self.config.admin_id)
        nonce = self.current_nonce()
        self.callback("cancel")
        self.callback("confirm:" + nonce)
        with self.sessions() as session:
            self.assertIsNone(session.get(Setting, "admin:26"))
        self.add_admin()
        self.message("/start", user=26)
        self.assertIn("🏫 کلاس‌ها", self.button_texts(self.last_payload_for(26)))

    def test_id_command_does_not_require_login(self):
        self.message("/id", user=26)
        self.assertEqual(self.latest_text(), "شناسه عددی حساب شما: 26")

    def test_disabling_feature_refreshes_existing_student_keyboard(self):
        self.message("0012345678")
        self.callback("feature:discounts:0")
        self.assertNotIn("🎁 تخفیف‌های من", self.button_texts(self.last_payload_for(25)))
        self.callback("feature:attendance:0")
        self.callback("feature:contacts:0")
        self.assertIn(COINS_STUDENT_BUTTON, self.button_texts(self.last_payload_for(25)))
        self.callback("feature:coins:0")
        remaining = ["🏫 کلاس‌های من", "📝 ثبت عدم حضور", HOMEWORK_BUTTON, "📅 جلسات من", REFRESH_BUTTON]
        self.assertEqual(self.button_texts(self.last_payload_for(25)), remaining)
        self.message("🎁 تخفیف‌های من")
        self.assertIn("غیرفعال", self.latest_text())
        self.assertEqual(self.button_texts(self.last_payload_for(25)), remaining)
        self.callback("feature:discounts:1")
        self.assertIn("🎁 تخفیف‌های من", self.button_texts(self.last_payload_for(25)))

    def test_rename_records_section_keeps_existing_data_and_refreshes_menu(self):
        self.message("0012345678")
        with self.sessions.begin() as session:
            apply_import(session, self.pending("discounts"))
        self.message("🎁 نام بخش تخفیف/رکوردها", user=self.config.admin_id)
        self.message("نتایج و دوره‌ها", user=self.config.admin_id)
        payload = self.last_payload_for(25)
        self.assertIn("🎁 نتایج و دوره‌ها", self.button_texts(payload))
        self.assertNotIn("🎁 تخفیف‌های من", self.button_texts(payload))
        self.callback(f"class:{self.class_id}")
        self.assertIn("ثبت رکورد در نتایج و دوره‌ها", self.button_texts(self.last_payload_for(self.config.admin_id)))
        self.message("🎁 نتایج و دوره‌ها")
        self.assertIn("OFF-20", self.latest_text())
        self.assertIn("نتایج و دوره‌ها", self.latest_text())
        self.callback("feature:discounts:0")
        self.assertNotIn("🎁 نتایج و دوره‌ها", self.button_texts(self.last_payload_for(25)))
        self.message("🎁 نتایج و دوره‌ها")
        self.assertIn("غیرفعال", self.latest_text())
        self.assertNotIn("OFF-20", self.latest_text())

    def test_records_title_validation(self):
        self.message("🎁 نام بخش تخفیف/رکوردها", user=self.config.admin_id)
        for title in ("", "الف\nب", "الف" * 61):
            self.message(title, user=self.config.admin_id)
            self.assertIn("بین ۱ تا ۶۰", self.latest_text())
        with self.sessions() as session:
            self.assertIsNone(session.get(Setting, "discounts_title"))

    def test_manual_records_support_multiple_titles_same_value_and_updates(self):
        self.callback("feature:excel:0")
        self.create_record("نمره ریاضی", "۲۰")
        nonce = self.create_record("نمره فیزیک", "۲۰")
        self.callback("confirm:" + nonce)
        self.assertIn("منقضی", self.latest_text())
        self.create_record("نمره ریاضی", "۱۹")
        with self.sessions() as session:
            rows = list(session.scalars(select(Discount).order_by(Discount.id)))
            self.assertEqual([(item.code, item.description) for item in rows], [("نمره ریاضی", "۱۹"), ("نمره فیزیک", "۲۰")])
        self.message("0012345678")
        self.message("🎁 تخفیف‌های من")
        self.assertIn("نمره ریاضی", self.latest_text())
        self.assertIn("نمره فیزیک", self.latest_text())
        self.message("🎁 تخفیف‌های من", user=26)
        self.assertNotIn("نمره ریاضی", self.latest_text())

    def test_manual_record_requires_confirmation_and_supports_empty_description(self):
        self.callback(f"record:{self.class_id}")
        self.message("0012345678", user=self.config.admin_id)
        self.message("OFF-10", user=self.config.admin_id)
        self.message("-", user=self.config.admin_id)
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Discount)), 0)
        self.callback("confirm:" + self.current_nonce())
        with self.sessions() as session:
            record = session.scalar(select(Discount))
            self.assertEqual((record.code, record.description), ("OFF-10", ""))

    def test_record_rejects_nonmembers_and_invalid_values(self):
        self.callback(f"record:{self.class_id}")
        self.message("9999999999", user=self.config.admin_id)
        self.assertIn("عضو فعال", self.latest_text())
        self.message("0012345678", user=self.config.admin_id)
        self.message("x" * 201, user=self.config.admin_id)
        self.assertIn("بین ۱ تا ۲۰۰", self.latest_text())
        self.message("OFF-10", user=self.config.admin_id)
        self.message("x" * 2001, user=self.config.admin_id)
        self.assertIn("بین ۱ تا ۲۰۰۰", self.latest_text())

    def test_record_revalidates_membership_and_feature_on_confirmation(self):
        self.callback(f"record:{self.class_id}")
        self.message("0012345678", user=self.config.admin_id)
        self.message("OFF-10", user=self.config.admin_id)
        self.message("دوره", user=self.config.admin_id)
        nonce = self.current_nonce()
        with self.sessions.begin() as session:
            session.get(Student, self.student_id).active = False
        self.callback("confirm:" + nonce)
        self.assertIn("عضو فعال", self.latest_text())
        with self.sessions.begin() as session:
            session.get(Student, self.student_id).active = True
        self.callback(f"record:{self.class_id}")
        self.message("0012345678", user=self.config.admin_id)
        self.message("OFF-10", user=self.config.admin_id)
        self.message("دوره", user=self.config.admin_id)
        nonce = self.current_nonce()
        with self.sessions.begin() as session:
            session.add(Setting(key="feature:discounts", value="0"))
        self.callback("confirm:" + nonce)
        self.assertIn("غیرفعال", self.latest_text())
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Discount)), 0)

    def test_added_admin_can_create_records_and_changes_survive_new_bot(self):
        from schoolbot.bot import Bot

        self.add_admin()
        self.runner.bot = Bot(self.config, self.api)
        self.create_record("OFF-NEW", "دوره جدید", user=26)
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(Discount)).code, "OFF-NEW")

    def test_unknown_student_exact_error(self):
        self.message("9999999999")
        self.assertEqual(self.latest_text(), "کد ملی وارد شده در سامانه موجود نیست؛ لطفاً با مدیریت تماس بگیرید.")

    def test_invalid_national_id(self):
        self.message("123")
        self.assertIn("۱۰ رقم", self.latest_text())

    def test_cannot_take_over_account(self):
        self.message("0012345678")
        self.message("0012345678", user=26)
        self.assertIn("قبلاً متصل", self.latest_text())

    def test_login_rate_limit_survives_start(self):
        for _index in range(10):
            self.message("9999999999")
        self.message("/start")
        self.message("0012345678")
        self.assertIn("بیش از حد", self.latest_text())
        with self.sessions() as session:
            self.assertIsNone(session.get(Student, self.student_id).chat_id)

    def test_remove_enrollment_keeps_history(self):
        with self.sessions.begin() as session:
            apply_import(session, self.pending())
        self.callback(f"remove:{self.class_id}")
        self.message("0012345678", user=self.config.admin_id)
        with self.sessions() as session:
            nonce = session.get(Conversation, self.config.admin_id).data["nonce"]
        self.callback("confirm:" + nonce)
        with self.sessions() as session:
            self.assertIsNone(session.get(Enrollment, (self.class_id, self.student_id)))
            self.assertEqual(session.scalar(select(func.count()).select_from(Attendance)), 1)

    def test_unlink_allows_rebinding_with_confirmation(self):
        self.message("0012345678")
        self.callback(f"unlink:{self.student_id}")
        with self.sessions() as session:
            nonce = session.get(Conversation, self.config.admin_id).data["nonce"]
            self.assertEqual(session.get(Student, self.student_id).chat_id, 25)
        self.callback("confirm:" + nonce)
        self.message("0012345678", user=26)
        with self.sessions() as session:
            self.assertEqual(session.get(Student, self.student_id).chat_id, 26)

    def test_failed_update_has_backoff_and_does_not_skip_order(self):
        self.api.error = BaleError(503)
        self.callback(f"upload:attendance:{self.class_id}")
        from unittest.mock import patch

        with patch.object(self.api, "download", side_effect=BaleError(503)):
            self.message(user=self.config.admin_id, document={"file_id": "id", "file_name": "file.xlsx"})
        self.assertFalse(self.runner.process_one())
        with self.sessions() as session:
            failed = session.get(Inbox, self.counter)
            self.assertEqual(failed.status, "pending")
            self.assertEqual(failed.attempts, 1)

    def test_prune_expired_pending_import(self):
        self.message("➕ ایجاد کلاس", user=self.config.admin_id)
        with self.sessions.begin() as session:
            session.get(Conversation, self.config.admin_id).updated_at = utcnow() - timedelta(hours=3)
        self.runner.prune()
        with self.sessions() as session:
            self.assertEqual(session.get(Conversation, self.config.admin_id).data, {})

    def test_inactive_student_rejected(self):
        with self.sessions.begin() as session:
            session.get(Student, self.student_id).active = False
        self.message("0012345678")
        self.assertIn("موجود نیست", self.latest_text())

    def test_admin_menu_and_security(self):
        self.message("/start", user=self.config.admin_id)
        self.message("➕ ایجاد کلاس", user=25)
        self.callback(f"rename:{self.class_id}", user=25)
        with self.sessions() as session:
            self.assertEqual(session.get(Classroom, self.class_id).title, "ریاضی")
        self.assertIn("مجاز نیست", self.latest_text())

    def test_groups_are_ignored(self):
        self.message("/start", user=self.config.admin_id, chat_type="group")
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Outbox)), 0)

    def test_create_rename_class(self):
        self.message("➕ ایجاد کلاس", user=self.config.admin_id)
        self.message("علوم", user=self.config.admin_id)
        self.callback(f"rename:{self.class_id}")
        self.message("ریاضی جدید", user=self.config.admin_id)
        with self.sessions() as session:
            self.assertEqual(session.get(Classroom, self.class_id).title, "ریاضی جدید")
            self.assertEqual(session.scalar(select(func.count()).select_from(Classroom)), 2)

    def test_contact_setting(self):
        self.message("⚙️ تنظیم راه‌های ارتباطی", user=self.config.admin_id)
        self.message("تماس: 02100000000", user=self.config.admin_id)
        self.message("0012345678")
        self.message("☎️ راه‌های ارتباطی")
        self.assertEqual(self.latest_text(), "تماس: 02100000000")

    def test_attendance_idempotent_and_changes_notify(self):
        self.message("0012345678")
        with self.sessions.begin() as session:
            self.assertEqual(apply_import(session, self.pending()), 1)
        with self.sessions.begin() as session:
            self.assertEqual(apply_import(session, self.pending()), 0)
            self.assertEqual(apply_import(session, self.pending(status="غایب")), 1)
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Attendance)), 1)
            notices = [item for item in session.scalars(select(Outbox)) if "وضعیت شما" in item.payload.get("text", "")]
            self.assertEqual(len(notices), 2)
            self.assertIn(excel.today()[1], notices[0].payload["text"])

    def test_unbound_student_no_notice(self):
        with self.sessions.begin() as session:
            apply_import(session, self.pending())
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Outbox)), 0)

    def test_in_bot_attendance_confirm_and_notify(self):
        self.message("0012345678")
        self.callback(f"attend:{self.class_id}")
        with self.sessions() as session:
            nonce = session.get(Conversation, self.config.admin_id).data["nonce"]
        self.callback(f"mark:{nonce}:{self.student_id}:absent")
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Attendance)), 0)
        self.callback(f"confirm:{nonce}")
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(Attendance)).status, "غایب")
            notices = [item for item in session.scalars(select(Outbox))
                       if item.payload.get("chat_id") == 25 and "وضعیت شما" in item.payload.get("text", "")]
            self.assertEqual(len(notices), 1)
        self.message("📋 حضور و غیاب من")
        self.assertIn("غایب", self.latest_text())
        self.assertIn("ریاضی", self.latest_text())

    def test_cancel_in_bot_attendance_rejects_old_buttons(self):
        self.callback(f"attend:{self.class_id}")
        with self.sessions() as session:
            nonce = session.get(Conversation, self.config.admin_id).data["nonce"]
        self.message("لغو", user=self.config.admin_id)
        self.callback(f"mark:{nonce}:{self.student_id}:present")
        self.assertIn("منقضی", self.latest_text())
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Attendance)), 0)

    def test_student_attendance_history_is_private(self):
        with self.sessions.begin() as session:
            apply_import(session, self.pending())
        self.message("📋 حضور و غیاب من", user=26)
        self.assertNotIn("ریاضی", self.latest_text())

    def test_import_rejects_foreign_student_atomically(self):
        pending = self.pending()
        pending["rows"].append({"national_id": "9999999999", "name": "ناشناس", "status": "حاضر"})
        with self.assertRaises(ValueError):
            with self.sessions.begin() as session:
                apply_import(session, pending)
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Attendance)), 0)

    def test_student_import_preserves_binding_and_membership(self):
        self.message("0012345678")
        with self.sessions.begin() as session:
            pending = self.pending("students")
            pending["rows"][0]["name"] = "نام جدید"
            apply_import(session, pending)
        with self.sessions() as session:
            student = session.get(Student, self.student_id)
            self.assertEqual(student.full_name, "نام جدید")
            self.assertEqual(student.chat_id, 25)
            self.assertEqual(session.scalar(select(func.count()).select_from(Enrollment)), 1)

    def test_discount_upsert_and_personal_menu(self):
        self.message("0012345678")
        with self.sessions.begin() as session:
            apply_import(session, self.pending("discounts"))
        with self.sessions.begin() as session:
            pending = self.pending("discounts")
            pending["rows"][0]["description"] = "دوره جدید"
            apply_import(session, pending)
        self.message("🎁 تخفیف‌های من")
        self.assertIn("OFF-20", self.latest_text())
        self.assertIn("دوره جدید", self.latest_text())
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Discount)), 1)

    def test_upload_requires_confirmation_and_is_single_use(self):
        self.api.content = excel.workbook("attendance", [("0012345678", "دانش آموز", "حاضر")], self.class_id,
                                          excel.today()[0].isoformat())
        self.callback(f"upload:attendance:{self.class_id}")
        self.message(user=self.config.admin_id, document={"file_id": "test", "file_name": "test.xlsx"})
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Attendance)), 0)
            nonce = session.get(Conversation, self.config.admin_id).data["nonce"]
        self.callback("confirm:" + nonce)
        self.callback("confirm:" + nonce)
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Attendance)), 1)
        self.assertIn("قبلاً انجام شده", self.latest_text())

    def test_cancel_upload(self):
        self.callback(f"upload:students:{self.class_id}")
        self.message("لغو", user=self.config.admin_id)
        with self.sessions() as session:
            self.assertEqual(session.get(Conversation, self.config.admin_id).data, {})

    def test_broadcast_media_confirm_deduplicated(self):
        self.message("0012345678")
        self.message("📢 پیام همگانی", user=self.config.admin_id)
        self.message(user=self.config.admin_id, photo=[{"file_id": "photo"}])
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Outbox).where(Outbox.method == "copyMessage")), 0)
            nonce = session.get(Conversation, self.config.admin_id).data["nonce"]
        self.callback("confirm:" + nonce)
        self.callback("confirm:" + nonce)
        with self.sessions() as session:
            copies = list(session.scalars(select(Outbox).where(Outbox.method == "copyMessage")))
            self.assertEqual(len(copies), 1)
            self.assertEqual(copies[0].payload["chat_id"], 25)

    def test_class_broadcast_excludes_other_class(self):
        self.message("0012345678")
        with self.sessions.begin() as session:
            classroom = Classroom(title="دیگر")
            session.add(classroom)
            session.flush()
            self.assertEqual(self.runner.bot.recipients(session, classroom.id), [])
            self.assertEqual(self.runner.bot.recipients(session, self.class_id), [25])

    def test_inbox_duplicate_not_processed_twice(self):
        update = self.message("/start", user=self.config.admin_id)
        self.runner.ingest([update])
        self.assertFalse(self.runner.process_one())
        self.assertEqual(self.runner.offset(), update["update_id"] + 1)
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Inbox)), 1)

    def test_ingest_preserves_valid_updates_in_a_malformed_out_of_order_batch(self):
        with self.assertLogs("schoolbot", level="WARNING"):
            self.runner.ingest([{"update_id": 11}, None, {"update_id": 9}, {"update_id": 11}, {"update_id": True}])
        self.assertEqual(self.runner.offset(), 12)
        self.runner.ingest([{"update_id": 10}])
        with self.sessions() as session:
            self.assertEqual(list(session.scalars(select(Inbox.update_id).order_by(Inbox.update_id))), [9, 11])
        with self.assertRaises(BaleError):
            self.runner.ingest({"unexpected": []})
        self.assertEqual(self.runner.offset(), 12)

    def test_numeric_prefix_callback_is_acknowledged(self):
        user = self.config.admin_id
        self.runner.ingest([{"update_id": 1, "callback_query": {
            "id": "10001", "from": {"id": user}, "data": "cancel",
            "message": {"chat": {"id": user, "type": "private"}},
        }}])
        self.assertTrue(self.runner.process_one())
        self.assertTrue(self.runner.deliver_one())
        self.assertEqual(self.api.calls[0], ("answerCallbackQuery", {"callback_query_id": "10001"}))

    def test_polling_delivers_start_despite_delayed_update_and_scheduler_error(self):
        admin = self.config.admin_id
        self.runner.ingest([{"update_id": 1}])
        with self.sessions.begin() as session:
            session.get(Inbox, 1).available_at = utcnow() + timedelta(minutes=5)
        start = {"update_id": 2, "message": {
            "from": {"id": admin}, "chat": {"id": admin, "type": "private"}, "text": "/start invite",
        }}
        refresh = {"update_id": 3, "message": {**start["message"], "text": REFRESH_BUTTON}}
        results = iter([True, True, [start, refresh], True, True, []])

        def stop_after_second_poll(method, payload=None):
            if method == "getUpdates" and payload["offset"] == 4:
                self.runner.stop()
            return next(results)

        with patch.object(self.runner, "initialize"), patch("schoolbot.runtime.signal.signal"), patch(
            "schoolbot.runtime.schedule_reminders", side_effect=RuntimeError("scheduler failure"),
        ), patch.object(self.api, "call", side_effect=stop_after_second_poll) as call:
            with self.assertLogs("schoolbot", level="INFO") as logs:
                self.runner.run()
        self.assertIn("getUpdates succeeded", " ".join(logs.output))
        self.assertIn("Meeting reminder job failed", " ".join(logs.output))
        self.assertEqual([entry.args[0] for entry in call.call_args_list],
                         ["getMe", "deleteWebhook", "getUpdates", "sendMessage", "sendMessage", "getUpdates"])
        self.assertIn("گزینه موردنظر", call.call_args_list[3].args[1]["text"])
        self.assertIn("منوی شما به‌روز شد", call.call_args_list[4].args[1]["text"])

    def test_delivery_retry_after_and_failure(self):
        self.message("/start", user=self.config.admin_id)
        self.api.error = BaleError(429, 30)
        self.runner.deliver_one()
        with self.sessions.begin() as session:
            item = session.scalar(select(Outbox))
            self.assertEqual(item.status, "pending")
            self.assertEqual(item.attempts, 1)
            item.available_at = utcnow() - timedelta(seconds=1)
        self.api.error = BaleError(403)
        self.runner.deliver_one()
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(Outbox)).status, "failed")

    def test_delivery_success_scrubs_payload(self):
        self.message("/start", user=self.config.admin_id)
        self.runner.deliver_one()
        with self.sessions() as session:
            item = session.scalar(select(Outbox))
            self.assertEqual(item.status, "sent")
            self.assertEqual(item.payload, {})

    def test_state_survives_runner_recreation(self):
        self.message("➕ ایجاد کلاس", user=self.config.admin_id)
        second = Runner(self.config, self.api, self.engine)
        with second.sessions() as session:
            self.assertEqual(session.get(Conversation, self.config.admin_id).data["mode"], "create")

    def test_expired_confirmation_rejected(self):
        self.message("📢 پیام همگانی", user=self.config.admin_id)
        self.message("خبر", user=self.config.admin_id)
        with self.sessions.begin() as session:
            state = session.get(Conversation, self.config.admin_id)
            nonce = state.data["nonce"]
            state.updated_at = utcnow() - timedelta(hours=3)
        self.callback("confirm:" + nonce)
        self.assertIn("منقضی", self.latest_text())


class ExcelTests(unittest.TestCase):
    def file(self, rows, kind="students", classroom_id=7):
        return excel.workbook(kind, rows, classroom_id, excel.today()[0].isoformat())

    def test_round_trip_leading_zero_and_metadata(self):
        content = self.file([("نام", "0012345678", "09123456789")])
        parsed = excel.parse(content, "students", 7)
        self.assertEqual(parsed["rows"][0]["national_id"], "0012345678")
        self.assertEqual(parsed["rows"][0]["phone"], "09123456789")

    def test_wrong_class_and_kind(self):
        content = self.file([("نام", "0012345678", "09123456789")])
        for kind, classroom in [("students", 8), ("attendance", 7)]:
            with self.assertRaises(excel.ExcelError):
                excel.parse(content, kind, classroom)

    def test_duplicate_row_rejected(self):
        row = ("نام", "0012345678", "09123456789")
        with self.assertRaises(excel.ExcelError):
            excel.parse(self.file([row, row]), "students", 7)

    def test_bad_status_and_blank_rejected(self):
        for status in ("", "تاخیر", "present"):
            with self.assertRaises(excel.ExcelError):
                excel.parse(self.file([("0012345678", "نام", status)], "attendance"), "attendance", 7)

    def test_formula_injection_is_text_on_export(self):
        content = self.file([("=HYPERLINK(1)", "0012345678", "09123456789")])
        book = load_workbook(BytesIO(content))
        self.assertEqual(book["داده‌ها"]["A2"].data_type, "s")
        book.close()

    def test_actual_formula_on_upload_rejected(self):
        content = self.file([("نام", "0012345678", "09123456789")])
        book = load_workbook(BytesIO(content))
        book["داده‌ها"]["A2"] = "=1+1"
        output = BytesIO()
        book.save(output)
        book.close()
        with self.assertRaises(excel.ExcelError):
            excel.parse(output.getvalue(), "students", 7)

    def test_max_rows(self):
        content = self.file([("نام", "0012345678", "09123456789"), ("نام دوم", "0012345679", "09123456789")])
        with self.assertRaises(excel.ExcelError):
            excel.parse(content, "students", 7, max_rows=1)

    def test_bad_archive_and_empty_file(self):
        for content in (b"not excel", self.file([])):
            with self.assertRaises(excel.ExcelError):
                excel.parse(content, "students", 7)

    def test_jalali_tehran_today(self):
        current_date, jalali = excel.today()
        import jdatetime

        self.assertEqual(jdatetime.datetime.strptime(jalali, "%Y/%m/%d").date().togregorian(), current_date)


if __name__ == "__main__":
    unittest.main()
