import secrets
from datetime import date

from sqlalchemy import select

from . import excel
from .education_access import ID_MAX, active_member, bound_student, identifiers
from .education_models import Homework, HomeworkSubmission
from .models import Classroom, Enrollment, Outbox, Student, utcnow
from .preferences import is_admin


HOMEWORK_ROUTES = {
    "homework": ("homework", 2), "hwnew": ("homework", 1), "submissions": ("homework", 3),
    "submission": ("homework", 3), "feedback": ("homework", 3),
}


def _version(submission):
    return [submission.submitted_at.isoformat(), submission.feedback] if submission is not None else None


class EducationHomeworkMixin:
    def education_homework_action(self, session, chat_id, user_id, state, classroom, action, values):
        from .education import jalali_day

        classroom_id = classroom.id
        back = f"ed:homework:{classroom_id}:0"
        scope = (user_id, classroom_id, "homework", 0)
        if action == "homework":
            self.education_page(session, chat_id, select(Homework).where(
                Homework.classroom_id == classroom_id,
            ).order_by(Homework.id.desc()), values[1], f"ed:homework:{classroom_id}", "تکالیف کلاس",
                lambda homework: (f"{homework.title} | {jalali_day(homework.due_date)}",
                                   f"ed:submissions:{classroom_id}:{homework.id}:0"),
                [("➕ تعریف تکلیف", f"ed:hwnew:{classroom_id}"), ("بازگشت", f"ed:class:{classroom_id}")], scope)
            return
        if action == "hwnew":
            self.education_form(session, chat_id, state, "homework",
                                "عنوان، مهلت شمسی و توضیح تکلیف را بفرستید. نمونه:\n"
                                "تمرین اول\n1405/09/15\nپاسخ سه سؤال را به‌صورت متن بفرستید.",
                                classroom_id=classroom_id)
            return
        homework = session.get(Homework, values[1])
        if homework is None or homework.classroom_id != classroom_id:
            self.say(session, chat_id, "تکلیف در این کلاس پیدا نشد.")
            return
        submissions = f"ed:submissions:{classroom_id}:{homework.id}"
        if action == "submissions":
            query = select(HomeworkSubmission).join(Student, Student.id == HomeworkSubmission.student_id).where(
                HomeworkSubmission.homework_id == homework.id,
            ).order_by(Student.full_name, Student.id)
            admin = is_admin(session, user_id, self.config.admin_id)
            if not admin:
                query = query.join(Enrollment, (Enrollment.student_id == Student.id) &
                                   (Enrollment.classroom_id == classroom_id)).where(Student.active.is_(True))
            self.education_page(session, chat_id, query, values[2], submissions,
                                f"تحویل‌های «{homework.title}» | مهلت: {jalali_day(homework.due_date)}\n{homework.instructions}",
                                lambda item: (f"{'✅ ' if item.feedback else ''}{session.get(Student, item.student_id).full_name}",
                                              f"ed:submission:{classroom_id}:{homework.id}:{item.student_id}"),
                                [("بازگشت", back)], scope)
            return
        student = active_member(session, classroom_id, values[2])
        history_only = student is None and is_admin(session, user_id, self.config.admin_id)
        if history_only:
            student = session.get(Student, values[2])
        submission = session.get(HomeworkSubmission, (homework.id, values[2]))
        if student is None or submission is None:
            self.say(session, chat_id, "تحویل موردنظر در کلاس قابل دسترسی نیست.")
            return
        if action == "feedback":
            if history_only:
                self.say(session, chat_id, "بازخورد فقط برای عضو فعال کلاس ثبت می‌شود.")
                return
            self.education_form(session, chat_id, state, "feedback",
                                f"بازخورد خصوصی برای «{student.full_name}» را بفرستید (حداکثر ۱۲۰۰ نویسه).",
                                classroom_id=classroom_id, homework_id=homework.id, student_id=student.id,
                                version=_version(submission))
        else:
            rows = [] if history_only else [[("ارسال بازخورد", f"ed:feedback:{classroom_id}:{homework.id}:{student.id}")]]
            body = f"تکلیف: {homework.title}\nدانش‌آموز: {student.full_name}\n\n{submission.body}"
            if submission.feedback is not None:
                body += f"\n\nبازخورد قبلی:\n{submission.feedback}"
            self.education_say(session, chat_id, body, rows + [[("بازگشت", f"{submissions}:0")]],
                               (user_id, classroom_id, "homework", 0 if history_only else student.id))

    def education_homework_form(self, session, chat_id, state, data, text):
        from .education import education_date, jalali_day

        classroom_id = data["classroom_id"]
        if data["operation"] == "homework":
            fields = [field.strip() for field in text.split("\n", 2)]
            try:
                if len(fields) != 3 or not 1 <= len(fields[0]) <= 200 or not 1 <= len(fields[2]) <= 1500:
                    raise ValueError
                deadline = education_date(fields[1])
                if deadline < excel.today()[0]:
                    raise ValueError
            except ValueError:
                self.say(session, chat_id, "عنوان، مهلت شمسی معتبر (امروز یا بعد) و توضیح را در سه بخش بفرستید.")
                return
            self.education_preview(session, chat_id, state, "homework",
                                   f"این تکلیف منتشر شود؟\nعنوان: {fields[0]}\nمهلت: {jalali_day(deadline)}\n{fields[2]}",
                                   f"ed:homework:{classroom_id}:0", classroom_id=classroom_id, title=fields[0],
                                   due_date=deadline.isoformat(), instructions=fields[2])
            return
        if len(text) > 1200:
            self.say(session, chat_id, "بازخورد حداکثر ۱۲۰۰ نویسه باشد.")
            return
        student = active_member(session, classroom_id, data["student_id"])
        if student is None:
            self.set_state(state)
            self.say(session, chat_id, "دانش‌آموز دیگر عضو فعال کلاس نیست.")
            return
        homework_id = data["homework_id"]
        self.education_preview(session, chat_id, state, "feedback",
                               f"بازخورد خصوصی برای «{student.full_name}» ثبت شود؟\n\n{text}",
                               f"ed:submission:{classroom_id}:{homework_id}:{student.id}", classroom_id=classroom_id,
                               homework_id=homework_id, student_id=student.id, version=data["version"], feedback=text)

    def education_homework_commit(self, session, chat_id, user_id, data):
        classroom_id = data["classroom_id"]
        if data["operation"] == "homework":
            deadline = date.fromisoformat(data["due_date"])
            if deadline < excel.today()[0]:
                self.say(session, chat_id, "مهلت تکلیف گذشته؛ تاریخ جدید وارد کنید.")
                return False
            session.add(Homework(classroom_id=classroom_id, actor_id=user_id, title=data["title"],
                                 due_date=deadline, instructions=data["instructions"]))
            return True
        homework = session.get(Homework, data["homework_id"])
        student = active_member(session, classroom_id, data["student_id"])
        submission = session.get(HomeworkSubmission, (data["homework_id"], data["student_id"]))
        if (homework is None or homework.classroom_id != classroom_id or student is None or submission is None
                or _version(submission) != data["version"]):
            self.say(session, chat_id, "تحویل یا عضویت تغییر کرده؛ دوباره از فهرست تحویل‌ها انتخاب کنید.")
            return False
        submission.feedback = data["feedback"]
        submission.reviewer_id = user_id
        if student.chat_id is not None and student.bale_user_id is not None:
            session.add(Outbox(method="sendMessage", dedupe_key=f"edfeedback:{homework.id}:{student.id}:{secrets.token_hex(12)}",
                               payload={"chat_id": student.chat_id, "text": f"📚 برای تکلیف «{homework.title}» بازخورد ثبت شد."
                                        "\nآن را در بخش «تکالیف من» ببینید."}))
        return True

    def student_homework_callback(self, session, chat_id, user_id, state, data):
        student = bound_student(session, user_id, chat_id)
        if student is None:
            self.say(session, chat_id, "ابتدا با حساب دانش‌آموز وارد شوید.")
            return
        from .education import jalali_day

        _, action, *args = data.split(":")
        if action == "confirm":
            pending = state.data
            if pending.get("mode") != "student_homework_confirm" or args != [pending.get("nonce")]:
                self.say(session, chat_id, "این درخواست منقضی شده یا قبلاً انجام شده است.")
                return
            self.set_state(state)
            self.student_homework_commit(session, chat_id, student, pending)
            return
        if action == "cancel":
            if state.data.get("mode", "").startswith("student_homework") and (
                    args == [state.data.get("nonce")] or (not args and "nonce" not in state.data)):
                self.set_state(state)
                self.say(session, chat_id, "تحویل لغو شد.")
            return
        values = identifiers(args, ID_MAX)
        if values is None:
            return
        self.set_state(state)
        if action == "list":
            query = select(Homework).join(Enrollment, Enrollment.classroom_id == Homework.classroom_id).where(
                Enrollment.student_id == student.id,
            ).order_by(Homework.id.desc())
            self.education_page(session, chat_id, query, values[0], "hw:list", "📚 تکالیف کلاس‌های شما",
                                lambda item: (f"{session.get(Classroom, item.classroom_id).title} | {item.title}",
                                              f"hw:open:{item.id}"))
            return
        if action not in ("open", "submit"):
            return
        homework = session.get(Homework, values[0])
        if homework is None or session.get(Enrollment, (homework.classroom_id, student.id)) is None:
            self.say(session, chat_id, "این تکلیف در کلاس‌های شما نیست.")
            return
        submission = session.get(HomeworkSubmission, (homework.id, student.id))
        editable = homework.due_date >= excel.today()[0] and (submission is None or submission.feedback is None)
        if action == "submit":
            if not editable:
                self.say(session, chat_id, "مهلت گذشته یا این تحویل قبلاً ارزیابی شده است.")
                return
            self.set_state(state, mode="student_homework_form", homework_id=homework.id, version=_version(submission))
            self.education_say(session, chat_id, "پاسخ متنی را در یک پیام (حداکثر ۲۲۰۰ نویسه) بفرستید.",
                               [[("لغو", "hw:cancel")]])
            return
        rows = [[("ویرایش پاسخ" if submission else "تحویل پاسخ", f"hw:submit:{homework.id}")]] if editable else []
        text = f"کلاس: {session.get(Classroom, homework.classroom_id).title}\nعنوان: {homework.title}" \
               f"\nمهلت: {jalali_day(homework.due_date)} (تا پایان روز به وقت تهران)\n\n{homework.instructions}"
        if submission:
            text += "\n\nپاسخ شما:\n" + submission.body
            text += "\n\nبازخورد:\n" + (submission.feedback or "هنوز ثبت نشده است.")
        self.education_say(session, chat_id, text, rows + [[("بازگشت", "hw:list:0")]])

    def student_homework_message(self, session, chat_id, user_id, state, text):
        data = state.data
        student = bound_student(session, user_id, chat_id)
        homework = session.get(Homework, data["homework_id"])
        if student is None or homework is None or session.get(Enrollment, (homework.classroom_id, student.id)) is None:
            self.set_state(state)
            self.say(session, chat_id, "دسترسی به تکلیف دیگر مجاز نیست.")
            return
        if data["mode"] != "student_homework_form":
            self.say(session, chat_id, "از دکمه تأیید یا لغو استفاده کنید.")
            return
        if not 1 <= len(text) <= 2200 or any(ord(char) < 32 and char not in "\n\r\t" for char in text):
            self.say(session, chat_id, "یک پاسخ متنی معتبر تا ۲۲۰۰ نویسه بفرستید.")
            return
        nonce = secrets.token_hex(8)
        self.set_state(state, **{**data, "mode": "student_homework_confirm", "body": text, "nonce": nonce})
        self.education_say(session, chat_id, f"پاسخ برای «{homework.title}» تحویل داده شود؟\n\n{text}",
                           [[("تأیید تحویل", f"hw:confirm:{nonce}"), ("لغو", f"hw:cancel:{nonce}")]])

    def student_homework_commit(self, session, chat_id, student, data):
        homework = session.get(Homework, data["homework_id"])
        if homework is None or session.get(Enrollment, (homework.classroom_id, student.id)) is None:
            self.say(session, chat_id, "این تکلیف در کلاس‌های شما نیست.")
            return
        submission = session.get(HomeworkSubmission, (homework.id, student.id))
        if (homework.due_date < excel.today()[0] or _version(submission) != data["version"]
                or (submission is not None and submission.feedback is not None)):
            self.say(session, chat_id, "مهلت گذشته یا نسخه تحویل تغییر کرده؛ تکلیف را دوباره باز کنید.")
            return
        if submission is None:
            session.add(HomeworkSubmission(homework_id=homework.id, student_id=student.id, body=data["body"]))
        else:
            submission.body = data["body"]
            submission.submitted_at = utcnow()
        self.education_say(session, chat_id, "✅ پاسخ تحویل داده شد.", [[("بازگشت به تکلیف", f"hw:open:{homework.id}")]])
