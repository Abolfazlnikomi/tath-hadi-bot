from .models import Setting


FEATURES = {
    "students": "مدیریت و بارگذاری کاربران",
    "attendance": "حضور و غیاب",
    "manual_attendance": "ثبت حضور و غیاب بدون اکسل",
    "discounts": "کدهای تخفیف",
    "excel": "دریافت و بارگذاری اکسل",
    "broadcast": "ارسال پیام همگانی و کلاسی",
    "contacts": "راه‌های ارتباطی",
}

TEXT_FEATURES = {
    "📋 حضور و غیاب من": "attendance",
    "🎁 تخفیف‌های من": "discounts",
    "☎️ راه‌های ارتباطی": "contacts",
    "⚙️ تنظیم راه‌های ارتباطی": "contacts",
    "📢 پیام همگانی": "broadcast",
    "👤 مدیریت دانش‌آموز": "students",
    "🎁 نام بخش تخفیف/رکوردها": "discounts",
}


def enabled(session, feature):
    setting = session.get(Setting, "feature:" + feature)
    return setting is None or setting.value != "0"


def callback_features(data):
    parts = data.split(":")
    action = parts[0]
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
    }.get(action, [])


def state_features(data):
    mode = data.get("mode", "")
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
    return []
