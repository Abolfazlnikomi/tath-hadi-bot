from .models import Setting


def save_setting(session, key, value):
    setting = session.get(Setting, key)
    if setting is None:
        session.add(Setting(key=key, value=value))
    else:
        setting.value = value


def is_admin(session, user_id, owner_id):
    return user_id == owner_id or session.get(Setting, f"admin:{user_id}") is not None


def records_title(session):
    setting = session.get(Setting, "discounts_title")
    return setting.value if setting else "تخفیف‌ها"


def records_button(session):
    title = records_title(session)
    return "🎁 تخفیف‌های من" if title == "تخفیف‌ها" else f"🎁 {title}"
