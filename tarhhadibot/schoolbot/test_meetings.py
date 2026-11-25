import base64
import unittest
from datetime import timedelta
from io import BytesIO

import jdatetime
from openpyxl import load_workbook
from sqlalchemy import create_engine, inspect, select

from . import excel
from .bot import REFRESH_BUTTON
from .config import Config
from .education_models import GuardianAccount, GuardianLink
from .meetings import TEHRAN, schedule_reminders
from .models import AbsenceRequest, Classroom, Conversation, Enrollment, Meeting, MeetingInvite, Outbox, Setting, Student, utcnow
from .runtime import Runner
from .parent_meetings import PHOTO_LIMIT, local_time, schedule_parent_meetings
from .parent_meeting_models import MeetingSection, ParentMeeting, ParentMeetingStage


class FakeAPI:
    content = b""

    def download(self, document):
        return self.content

    def validate_photo(self, file_id, limit):
        self.photo = (file_id, limit)


class MeetingTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.runner = Runner(Config("test-token", "unused"), self.api, create_engine("sqlite://"))
        self.addCleanup(self.runner.engine.dispose)
        self.runner.initialize()
        with self.runner.sessions.begin() as session:
            room = Classroom(title="ریاضی")
            students = [Student(national_id=code, full_name=code, phone="09123456789",
                                bale_user_id=user, chat_id=user) for code, user in
                        (("0012345678", 101), ("0012345679", 102))]
            session.add_all([room, *students])
            session.flush()
            self.room_id = room.id
            self.student_ids = [student.id for student in students]
            session.add_all([Enrollment(classroom_id=room.id, student_id=student.id) for student in students])

    def send(self, user, text=None, callback=None, document=None, photo=None):
        sender = {"id": user}
        message = {"chat": {"type": "private", "id": user}, "from": sender}
        if text is not None:
            message["text"] = text
        if document is not None:
            message["document"] = document
        if photo is not None:
            message["photo"] = photo
        update = ({"callback_query": {"id": "20", "from": sender, "message": message, "data": callback}}
                  if callback is not None else {"message": message})
        with self.runner.sessions.begin() as session:
            self.runner.bot.handle(session, update)

    def last(self, user, method="sendMessage"):
        with self.runner.sessions() as session:
            return next(item.payload for item in session.scalars(select(Outbox).where(Outbox.method == method)
                        .order_by(Outbox.id.desc())) if item.payload.get("chat_id") == user)

    def create(self, audience):
        admin = self.runner.config.admin_id
        self.send(admin, "📅 جلسات")
        self.send(admin, callback="mt:classes:0")
        self.send(admin, callback=f"mt:choose:{self.room_id}")
        self.send(admin, "جلسه اولیا")
        self.send(admin, "سالن مدرسه")
        local = (utcnow() + timedelta(days=5)).astimezone(TEHRAN)
        date_text = jdatetime.datetime.fromgregorian(datetime=local).strftime("%Y/%m/%d %H:%M")
        self.send(admin, date_text)
        self.send(admin, callback=f"mt:audience:{audience}")
        if audience == "e":
            self.api.content = excel.workbook("meeting_invites", [("0000000000",)], self.room_id)
            self.send(admin, document={"file_id": "fake"})
            self.assertIn("پذیرفته نشد", self.last(admin)["text"])
            self.api.content = excel.workbook("meeting_invites", [("0012345678",)], self.room_id)
            self.send(admin, document={"file_id": "fake"})
        confirmation = self.last(admin)["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.send(admin, callback=confirmation)
        self.send(admin, callback=confirmation)
        with self.runner.sessions() as session:
            return session.scalars(select(Meeting)).one().id

    def test_parent_multiclass_three_responses_edit_and_cancel(self):
        admin = self.runner.config.admin_id
        with self.runner.sessions.begin() as session:
            room = Classroom(title="علوم")
            session.add(room)
            session.flush()
            second = room.id
            session.add(Enrollment(classroom_id=second, student_id=self.student_ids[0]))
            session.add(GuardianAccount(user_id=201, chat_id=201))
            session.flush()
            for student_id in self.student_ids:
                session.add(GuardianLink(student_id=student_id, guardian_id=201))
        self.send(admin, callback="pm:section")
        self.send(admin, "آموزشی")
        with self.runner.sessions() as session:
            section = session.scalar(select(MeetingSection.id))
        self.send(admin, callback=f"pm:pick:{section}")
        for room_id in (self.room_id, second):
            self.send(admin, callback=f"pm:toggle:{room_id}:0")
        self.send(admin, callback="pm:next")
        self.assertIn("امروز در تهران", self.last(admin)["text"])
        self.send(admin, f"معارفه\nسالن\n{local_time(utcnow() + timedelta(days=5))}")
        self.send(admin, photo=[{"file_id": "big", "file_size": PHOTO_LIMIT + 1}])
        self.assertIn("حداکثر ۱ مگابایت", self.last(admin)["text"])
        self.send(admin, photo=[{"file_id": "small", "file_size": 10}])
        self.assertEqual(self.api.photo, ("small", PHOTO_LIMIT))
        def confirm():
            self.send(admin, callback=self.last(admin)["reply_markup"]["inline_keyboard"][0][0]["callback_data"])
        confirm()
        with self.runner.sessions.begin() as session:
            meeting = session.scalars(select(ParentMeeting)).one()
            meeting_id = meeting.id
            from .meetings import utc
            start = utc(meeting.starts_at)
            schedule_parent_meetings(session, start - timedelta(days=3))
            schedule_parent_meetings(session, start - timedelta(days=1))
            schedule_parent_meetings(session, start - timedelta(days=1))
            stages = list(session.scalars(select(ParentMeetingStage)))
            self.assertEqual({item.stage for item in stages}, {"new", "one", "three"})
            self.assertEqual(len(stages), 3)
        self.send(201, "/family_meetings")
        self.assertIn("آموزشی", self.last(201)["reply_markup"]["inline_keyboard"][0][0]["text"])
        self.send(202, callback=f"pf:r:{meeting_id}:1:new:yes")
        self.assertIn("/family", self.last(202)["text"])
        for stage, answer in (("new", "yes"), ("three", "no"), ("one", "yes")):
            self.send(201, callback=f"pf:r:{meeting_id}:1:{stage}:{answer}")
            self.assertIn("ثبت شد", self.last(201)["text"])
        self.send(admin, callback=f"pm:export:{meeting_id}")
        book = load_workbook(BytesIO(base64.b64decode(self.last(admin, "sendDocument")["_file"]["content"])))
        report = book["پاسخ جامع والدین"]
        self.assertEqual([report.cell(2, index).value for index in (4, 5, 6)],
                         ["شرکت می‌کنم", "شرکت نمی‌کنم", "شرکت می‌کنم"])
        book.close()
        self.send(admin, callback=f"pm:edit:{meeting_id}")
        self.send(admin, callback="pm:next")
        self.send(admin, "عنوان جدید\n-\n-")
        self.send(admin, "حذف عکس")
        confirm()
        self.send(201, callback=f"pf:r:{meeting_id}:1:new:no")
        self.assertIn("منقضی", self.last(201)["text"])
        self.send(admin, callback=f"pm:delete:{meeting_id}")
        confirm()
        self.send(201, callback=f"pf:view:{meeting_id}")
        self.assertIn("در دسترس نیست", self.last(201)["text"])
        with self.runner.sessions() as session:
            self.assertTrue(session.get(ParentMeeting, meeting_id).cancelled)
            self.assertEqual(session.get(ParentMeetingStage, (meeting_id, 1, 201, "new")).response, "yes")

    def test_class_and_reminders(self):
        meeting_id = self.create("a")
        with self.runner.sessions.begin() as session:
            self.assertEqual(len(session.scalars(select(MeetingInvite)).all()), 2)
            meeting = session.get(Meeting, meeting_id)
            from .meetings import utc
            start = utc(meeting.starts_at)
            schedule_reminders(session, start - timedelta(days=3))
            schedule_reminders(session, start - timedelta(days=3))
            schedule_reminders(session, start - timedelta(days=1))
        with self.runner.sessions() as session:
            notices = session.scalars(select(Outbox).where(Outbox.dedupe_key.startswith("meeting:"))).all()
            self.assertEqual(len(notices), 6)
            self.assertEqual(sum("reply_markup" in row.payload for row in notices), 2)
        self.send(101, "📅 جلسات من")
        self.assertIn("سالن مدرسه", self.last(101)["text"])
        self.send(101, callback=f"mr:{meeting_id}:yes")
        self.send(102, callback=f"mr:{meeting_id}:no")
        admin = self.runner.config.admin_id
        self.send(admin, callback=f"mt:export:{meeting_id}")
        book = load_workbook(BytesIO(base64.b64decode(self.last(admin, "sendDocument")["_file"]["content"])))
        self.assertEqual(book["شرکت می‌کنم"]["A2"].value, "0012345678")
        self.assertEqual(book["شرکت نمی‌کنم"]["A2"].value, "0012345679")
        self.assertEqual(book["بدون پاسخ"].max_row, 1)
        book.close()

    def test_excel_invites_only_selected_student(self):
        meeting_id = self.create("e")
        self.send(102, "📅 جلسات من")
        self.assertIn("جلسه‌ای برای شما ثبت نشده", self.last(102)["text"])
        self.send(102, callback=f"mr:{meeting_id}:yes")
        self.assertIn("در دسترس نیست", self.last(102)["text"])
        with self.runner.sessions() as session:
            invite = session.scalars(select(MeetingInvite)).one()
            self.assertEqual(invite.student_id, self.student_ids[0])
            self.assertIsNone(invite.response)

    def test_refresh_respects_role_and_feature_flags(self):
        def buttons(user):
            markup = self.last(user)["reply_markup"]
            return {button["text"] for row in markup["keyboard"] for button in row}

        admin = self.runner.config.admin_id
        self.send(admin, callback=f"mt:choose:{self.room_id}")
        self.send(admin, REFRESH_BUTTON)
        self.assertIn("📅 جلسات", buttons(admin))
        self.assertIn("👥 مدیریت ادمین‌ها", buttons(admin))
        with self.runner.sessions.begin() as session:
            self.assertEqual(session.get(Conversation, admin).data["mode"], "meeting_topic")
            session.add(Setting(key="feature:meetings", value="0"))
        self.send(101, "/refresh")
        self.assertIn(REFRESH_BUTTON, buttons(101))
        self.assertNotIn("📅 جلسات من", buttons(101))
        self.assertNotIn("👥 مدیریت ادمین‌ها", buttons(101))
        self.send(103, REFRESH_BUTTON)
        self.assertEqual(self.last(103)["reply_markup"], {"remove_keyboard": True})

    def test_refresh_accepts_existing_keyboard_labels(self):
        for label in ("🔄 به‌روزرسانی", "بروزرسانی بات", "به روز رساني بات", "/refresh"):
            with self.subTest(label=label):
                self.send(101, label)
                self.assertIn("منوی شما به‌روز شد", self.last(101)["text"])

    def test_initialize_upgrades_legacy_classrooms(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE classrooms (id INTEGER PRIMARY KEY, title VARCHAR(200) UNIQUE NOT NULL)")
            connection.exec_driver_sql("INSERT INTO classrooms (id, title) VALUES (1, 'Legacy class')")
        runner = Runner(self.runner.config, self.api, engine)
        runner.initialize()
        runner.initialize()
        with runner.sessions() as session:
            room = session.get(Classroom, 1)
            self.assertEqual(room.title, "Legacy class")
            self.assertEqual((room.instructor, room.location, room.schedule), (None, None, None))
            self.assertEqual(session.get(Setting, "schema_version").value, "2")
        self.assertTrue(inspect(engine).has_table("meeting_invites"))

    def test_absence_confirmation_and_excel(self):
        self.send(101, callback=f"absence_cls:{self.room_id}")
        self.send(101, "فردا")
        self.send(101, "بیماری")
        confirmation = self.last(101)["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.send(101, callback=confirmation)
        self.send(101, callback=confirmation)
        with self.runner.sessions() as session:
            absence = session.scalars(select(AbsenceRequest)).one()
            self.assertEqual(absence.student_id, self.student_ids[0])
            self.assertEqual(absence.reason, "بیماری")
        admin = self.runner.config.admin_id
        self.send(admin, "📋 گزارش درخواست‌های عدم حضور")
        report = load_workbook(BytesIO(base64.b64decode(self.last(admin, "sendDocument")["_file"]["content"])))
        self.assertEqual(report.active["C2"].value, "0012345678")
        report.close()
