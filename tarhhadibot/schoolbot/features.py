from .models import Setting


COINS_ADMIN_BUTTON = "🪙 امتیازات"
COINS_STUDENT_BUTTON = "🪙 سکه‌های هادی"
REMINDER_BUTTON = "🔔 یادآوری هوشمند"
CONSULTATION_BUTTON = "🗓 مشاوره"


FEATURES = {
    "students": "مدیریت و بارگذاری کاربران",
    "attendance": "حضور و غیاب",
    "manual_attendance": "ثبت حضور و غیاب بدون اکسل",
    "absence": "اطلاع‌رسانی عدم حضور",
    "discounts": "کدهای تخفیف",
    "excel": "دریافت و بارگذاری اکسل",
    "broadcast": "ارسال پیام همگانی و کلاسی",
    "contacts": "راه‌های ارتباطی",
    "meetings": "جلسات و یادآوری",
    "coins": "امتیازات و سکه‌های هادی",
    "reminders": "یادآوری هوشمند",
    "consultations": "مشاوره",
}

TEXT_FEATURES = {
    "📅 جلسات": "meetings",
    "📅 جلسات من": "meetings",
    "📋 حضور و غیاب من": "attendance",
    "📝 ثبت عدم حضور": "absence",
    "📋 گزارش درخواست‌های عدم حضور": "absence",
    "🎁 تخفیف‌های من": "discounts",
    "☎️ راه‌های ارتباطی": "contacts",
    "⚙️ تنظیم راه‌های ارتباطی": "contacts",
    "📢 پیام همگانی": "broadcast",
    "👤 مدیریت دانش‌آموز": "students",
    "🎁 نام بخش تخفیف/رکوردها": "discounts",
    COINS_ADMIN_BUTTON: "coins",
    COINS_STUDENT_BUTTON: "coins",
    REMINDER_BUTTON: "reminders",
    CONSULTATION_BUTTON: "consultations",
}


def enabled(session, feature):
    setting = session.get(Setting, "feature:" + feature)
    return setting is None or setting.value != "0"


def callback_features(data):
    parts = data.split(":")
    action = parts[0]
    if action == "coins":
        return ["coins", "excel"] if parts[1:2] == ["report"] else ["coins"]
    if action in ("upload", "export", "template") and len(parts) == 3:
        return ["excel", parts[1]]
    return {
        "attend": ["attendance", "manual_attendance"],
        "mark": ["attendance", "manual_attendance"],
        "broadcast": ["broadcast"],
        "remove": ["students"],
        "toggle": ["students"],
        "unlink": ["students"],
        "record": ["discounts"],
        "absence": ["absence"],
        "absence_list": ["absence"],
    }.get(action, [])


def state_features(data):
    mode = data.get("mode", "")
    if mode.startswith("coins_"):
        return ["coins"]
    if mode == "upload":
        return ["excel", data["kind"]]
    if mode == "confirm_import":
        return [data["pending"]["kind"], data.get("source", "excel")]
    if mode == "attendance":
        return ["attendance", "manual_attendance"]
    if mode in ("broadcast", "confirm_broadcast"):
        return ["broadcast"]
    if mode in ("student_lookup", "remove", "confirm_remove", "confirm_student"):
        return ["students"]
    if mode == "contacts":
        return ["contacts"]
    if mode in ("records_title", "record_student", "record_code", "record_description"):
        return ["discounts"]
    if mode in ("absence_class", "absence_date", "absence_reason", "confirm_absence"):
        return ["absence"]
    return []
