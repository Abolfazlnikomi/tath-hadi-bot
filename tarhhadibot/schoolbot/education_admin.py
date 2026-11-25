from sqlalchemy import select

from . import excel
from .education_access import ID_MAX, PERMISSIONS, USER_MAX, active_member, class_students, identifiers
from .education_models import AbsenceFollowup, GuardianAccount, GuardianLink, Mentor, MentorClass
from .models import Attendance, Classroom, Student


ADMIN_ROUTES = {
    "manage", "add", "mentor", "active", "assign", "mclass", "perm", "unassign",
    "guardians", "guardian", "glink", "gunlink", "familylog", "familyview", "referralqueue",
}


class EducationAdminMixin:
    def education_admin_action(self, session, chat_id, user_id, state, action, args):
        limits = {
            "manage": (ID_MAX,), "add": (), "mentor": (USER_MAX,), "active": (USER_MAX, 1),
            "assign": (USER_MAX, ID_MAX), "mclass": (USER_MAX, ID_MAX),
            "perm": (USER_MAX, ID_MAX, len(PERMISSIONS) - 1, 1), "unassign": (USER_MAX, ID_MAX),
            "guardians": (ID_MAX, ID_MAX), "guardian": (ID_MAX, ID_MAX, ID_MAX),
            "glink": (ID_MAX, ID_MAX), "gunlink": (ID_MAX, ID_MAX, USER_MAX),
            "familylog": (ID_MAX, ID_MAX), "familyview": (ID_MAX, ID_MAX), "referralqueue": (ID_MAX,),
        }
        values = identifiers(args, *limits[action])
        if values is None:
            return
        self.set_state(state)
        if action == "manage":
            self.education_page(session, chat_id, select(Mentor).order_by(Mentor.name, Mentor.user_id),
                                values[0], "ed:manage", "مدیریت مربیان",
                                lambda mentor: (f"{'فعال' if mentor.active else 'غیرفعال'} | {mentor.name}",
                                                f"ed:mentor:{mentor.user_id}"),
                                [("➕ ثبت مربی", "ed:add"), ("🎓 کلاس‌ها", "ed:classes:0"),
                                 ("ارجاع‌های باز همه کلاس‌ها", "ed:referralqueue:0")])
            return
        if action == "referralqueue":
            if not self.allow(session, chat_id, ["attendance"]):
                return
            self.education_page(session, chat_id, select(Attendance).join(AbsenceFollowup).where(
                Attendance.status == "غایب", AbsenceFollowup.referred.is_(True), AbsenceFollowup.resolved.is_(False),
            ).order_by(Attendance.session_date.desc(), Attendance.id.desc()), values[0], "ed:referralqueue",
                "ارجاع‌های باز همه کلاس‌ها",
                lambda item: (f"{session.get(Classroom, item.classroom_id).title} | {item.jalali_date}",
                              f"ed:absenceview:{item.classroom_id}:{item.id}"), [("بازگشت", "ed:manage:0")])
            return
        if action == "add":
            self.education_form(session, chat_id, state, "mentor_add", "نام مربی و شناسه عددی حساب بله را در دو خط بفرستید:"
                                "\nنام و نام خانوادگی\nشناسه عددی (از دستور /id در بات)")
            return
        if action in ("guardians", "guardian", "glink", "gunlink", "familylog", "familyview"):
            self.education_guardian_admin(session, chat_id, user_id, state, action, values)
            return
        mentor = session.get(Mentor, values[0])
        if mentor is None:
            self.say(session, chat_id, "مربی پیدا نشد.")
            return
        if action == "active":
            mentor.active = bool(values[1])
            self.audit(session, user_id, "mentor_status", mentor_id=mentor.user_id, active=mentor.active)
        if action in ("mentor", "active"):
            self.education_say(session, chat_id, f"مربی: {mentor.name}\nشناسه: {mentor.user_id}"
                               f"\nوضعیت: {'فعال' if mentor.active else 'غیرفعال'}", [
                [("کلاس‌ها و مجوزها", f"ed:assign:{mentor.user_id}:0")],
                [("غیرفعال‌کردن" if mentor.active else "فعال‌کردن", f"ed:active:{mentor.user_id}:{int(not mentor.active)}")],
                [("بازگشت به مربیان", "ed:manage:0")],
            ])
            return
        if action == "assign":
            assigned = set(session.scalars(select(MentorClass.classroom_id).where(MentorClass.mentor_id == mentor.user_id)))
            self.education_page(session, chat_id, select(Classroom).order_by(Classroom.id), values[1],
                                f"ed:assign:{mentor.user_id}", f"کلاس‌ها و مجوزهای «{mentor.name}»",
                                lambda classroom: (f"{'✅' if classroom.id in assigned else '➕'} {classroom.title}",
                                                   f"ed:mclass:{mentor.user_id}:{classroom.id}"),
                                [("بازگشت", f"ed:mentor:{mentor.user_id}")])
            return
        classroom = session.get(Classroom, values[1])
        if classroom is None:
            self.say(session, chat_id, "کلاس پیدا نشد.")
            return
        key = (mentor.user_id, classroom.id)
        assignment = session.get(MentorClass, key)
        if action == "unassign":
            if assignment is not None:
                session.delete(assignment)
                self.audit(session, user_id, "mentor_class_remove", mentor_id=mentor.user_id, classroom_id=classroom.id)
            assignment = None
        elif action == "perm":
            bit, label = list(PERMISSIONS.values())[values[2]]
            if assignment is None:
                self.say(session, chat_id, "ابتدا مربی را به این کلاس اختصاص دهید.")
                return
            assignment.permissions = assignment.permissions | bit if values[3] else assignment.permissions & ~bit
            self.audit(session, user_id, "mentor_permission", mentor_id=mentor.user_id, classroom_id=classroom.id,
                       permission=label, enabled=bool(values[3]))
        rows = []
        if assignment is None:
            self.education_preview(session, chat_id, state, "assign",
                                   f"«{mentor.name}» به کلاس «{classroom.title}» اضافه شود؟"
                                   "\nمجوزها ابتدا خاموش‌اند و باید جداگانه فعال شوند.",
                                   f"ed:assign:{mentor.user_id}:0", mentor_id=mentor.user_id, classroom_id=classroom.id)
            return
        for index, (bit, label) in enumerate(PERMISSIONS.values()):
            granted = bool(assignment.permissions & bit)
            rows.append([(f"{'✅' if granted else '⛔'} {label}", f"ed:perm:{mentor.user_id}:{classroom.id}:{index}:{int(not granted)}")])
        rows.extend([[("قطع دسترسی به این کلاس", f"ed:unassign:{mentor.user_id}:{classroom.id}")],
                     [("بازگشت", f"ed:assign:{mentor.user_id}:0")]])
        self.education_say(session, chat_id, f"مجوزهای «{mentor.name}» در کلاس «{classroom.title}»:", rows)

    def education_guardian_admin(self, session, chat_id, user_id, state, action, values):
        classroom_id = values[0]
        if session.get(Classroom, classroom_id) is None:
            self.say(session, chat_id, "کلاس پیدا نشد.")
            return
        if action in ("familylog", "familyview"):
            self.education_family_history(session, chat_id, classroom_id, action, values[1])
            return
        if action == "guardians":
            self.education_page(session, chat_id, class_students(classroom_id).order_by(
                Student.full_name, Student.id,
            ), values[1], f"ed:guardians:{classroom_id}", "اولیای دانش‌آموزان",
                lambda student: (f"{student.full_name} | {student.national_id}", f"ed:guardian:{classroom_id}:{student.id}:0"),
                [("بازگشت", f"ed:class:{classroom_id}")])
            return
        student = active_member(session, classroom_id, values[1])
        if student is None:
            self.say(session, chat_id, "دانش‌آموز عضو فعال کلاس نیست.")
            return
        prefix = f"{classroom_id}:{student.id}"
        if action == "glink":
            self.education_form(session, chat_id, state, "guardian_add",
                                "شناسه عددی ولی را که با دستور /id گرفته‌اید بفرستید. قبل از ثبت، رابطه را بررسی کنید.",
                                classroom_id=classroom_id, student_id=student.id)
            return
        if action == "gunlink":
            self.education_preview(session, chat_id, state, "guardian_remove",
                                   f"ارتباط حساب {values[2]} با «{student.full_name}» قطع شود؟",
                                   f"ed:guardian:{prefix}:0", classroom_id=classroom_id,
                                   student_id=student.id, guardian_id=values[2])
            return
        self.education_page(session, chat_id, select(GuardianAccount).join(GuardianLink).where(
            GuardianLink.student_id == student.id,
        ).order_by(GuardianAccount.user_id), values[2], f"ed:guardian:{prefix}", f"اولیای «{student.full_name}»",
            lambda account: (f"قطع ارتباط {account.user_id} ({'دریافت فعال' if account.chat_id is not None else 'دریافت غیرفعال'})",
                             f"ed:gunlink:{prefix}:{account.user_id}"),
            [("اتصال ولی جدید", f"ed:glink:{prefix}"), ("بازگشت", f"ed:guardians:{classroom_id}:0")])

    def education_admin_form(self, session, chat_id, state, data, text):
        if data["operation"] == "mentor_add":
            fields = text.splitlines()
            name = " ".join(fields[0].split()) if len(fields) == 2 else ""
            values = identifiers([excel.normalize(fields[1])], USER_MAX) if name and len(name) <= 100 else None
            if not values or not values[0]:
                self.say(session, chat_id, "نام (حداکثر ۱۰۰ نویسه) و شناسه عددی مثبت را در دو خط بفرستید.")
                return
            self.education_preview(session, chat_id, state, "mentor_add",
                                   f"«{name}» با شناسه {values[0]} به‌عنوان مربی ثبت شود؟",
                                   "ed:manage:0", mentor_id=values[0], name=name)
        else:
            values = identifiers([excel.normalize(text)], USER_MAX)
            student = active_member(session, data["classroom_id"], data["student_id"])
            if student is None:
                self.set_state(state)
                self.say(session, chat_id, "دانش‌آموز دیگر عضو فعال کلاس نیست.")
                return
            if not values or not values[0]:
                self.say(session, chat_id, "شناسه عددی مثبت حساب ولی را بفرستید.")
                return
            self.education_preview(session, chat_id, state, "guardian_add",
                                   f"حساب {values[0]} به‌عنوان ولی «{student.full_name}» ثبت شود؟"
                                   "\nاین ارتباط برای همه کلاس‌های دانش‌آموز معتبر است.",
                                   f"ed:guardian:{data['classroom_id']}:{student.id}:0",
                                   classroom_id=data["classroom_id"], student_id=student.id, guardian_id=values[0])

    def education_admin_commit(self, session, chat_id, user_id, state, data):
        operation = data["operation"]
        if operation == "mentor_add":
            if session.get(Mentor, data["mentor_id"]) is not None:
                self.say(session, chat_id, "این حساب قبلاً ثبت شده است.")
                return
            session.add(Mentor(user_id=data["mentor_id"], name=data["name"]))
            self.audit(session, user_id, "mentor_create", mentor_id=data["mentor_id"])
            self.education_say(session, chat_id, "مربی ثبت شد. کلاس‌ها و مجوزها را تعیین کنید.",
                               [[("کلاس‌ها و مجوزها", f"ed:assign:{data['mentor_id']}:0")]])
        elif operation == "assign":
            key = data["mentor_id"], data["classroom_id"]
            if session.get(Mentor, key[0]) is None or session.get(Classroom, key[1]) is None:
                self.say(session, chat_id, "مربی یا کلاس پیدا نشد.")
                return
            if session.get(MentorClass, key) is None:
                session.add(MentorClass(mentor_id=key[0], classroom_id=key[1]))
                self.audit(session, user_id, "mentor_class_add", mentor_id=key[0], classroom_id=key[1])
            self.education_say(session, chat_id, "تخصیص ثبت شد.", [[("تنظیم مجوزها", f"ed:mclass:{key[0]}:{key[1]}")]])
        else:
            student = active_member(session, data["classroom_id"], data["student_id"])
            if student is None:
                self.say(session, chat_id, "دانش‌آموز دیگر عضو فعال کلاس نیست.")
                return
            guardian_id = data["guardian_id"]
            link = session.get(GuardianLink, (student.id, guardian_id))
            if operation == "guardian_add" and link is None:
                if session.get(GuardianAccount, guardian_id) is None:
                    session.add(GuardianAccount(user_id=guardian_id))
                    session.flush()
                session.add(GuardianLink(student_id=student.id, guardian_id=guardian_id))
                self.audit(session, user_id, "guardian_link", student_id=student.id, guardian_id=guardian_id)
            if operation == "guardian_remove" and link is not None:
                session.delete(link)
                self.audit(session, user_id, "guardian_unlink", student_id=student.id, guardian_id=guardian_id)
                session.flush()
                if session.scalar(select(GuardianLink.student_id).where(
                        GuardianLink.guardian_id == guardian_id).limit(1)) is None:
                    session.get(GuardianAccount, guardian_id).chat_id = None
            self.education_say(session, chat_id, "ثبت شد.", [[("بازگشت", data["back"])]])
