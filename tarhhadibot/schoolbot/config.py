import os
import math
from dataclasses import dataclass, field

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    database_url: str = field(repr=False)
    admin_id: int = 1891614962
    send_interval: float = 0.15
    max_file_bytes: int = 5 * 1024 * 1024
    max_excel_rows: int = 10000
    contact_text: str = "برای دریافت اطلاعات با مدیریت تماس بگیرید."

    @classmethod
    def from_env(cls):
        load_dotenv()
        token = os.getenv("BALE_BOT_TOKEN", "")
        target = os.getenv("DATABASE_TARGET", "default").strip().lower()
        if target not in ("default", "local", "main"):
            raise ValueError("DATABASE_TARGET باید default، local یا main باشد.")
        variable = "DATABASE_URL" if target == "default" else f"{target.upper()}_DATABASE_URL"
        database_url = os.getenv(variable, "").strip()
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        if not token or token == "replace_with_a_new_token":
            raise ValueError("توکن جدید را در BALE_BOT_TOKEN تنظیم کنید.")
        if not database_url.startswith("postgresql+psycopg://"):
            raise ValueError(f"{variable} باید اتصال PostgreSQL با درایور psycopg باشد.")
        interval = float(os.getenv("SEND_INTERVAL", "0.15"))
        if not math.isfinite(interval) or interval < 0.05:
            raise ValueError("SEND_INTERVAL باید حداقل 0.05 باشد.")
        max_file_bytes = int(os.getenv("MAX_FILE_BYTES", str(5 * 1024 * 1024)))
        max_excel_rows = int(os.getenv("MAX_EXCEL_ROWS", "10000"))
        if max_file_bytes <= 0 or max_excel_rows <= 0:
            raise ValueError("محدودیت حجم فایل و تعداد ردیف باید مثبت باشد.")
        return cls(
            token=token,
            database_url=database_url,
            admin_id=int(os.getenv("ADMIN_USER_ID", "1891614962")),
            send_interval=interval,
            max_file_bytes=max_file_bytes,
            max_excel_rows=max_excel_rows,
            contact_text=os.getenv("CONTACT_TEXT", cls.contact_text),
        )
