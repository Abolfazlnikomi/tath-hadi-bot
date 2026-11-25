import logging
import signal
import time
from datetime import timedelta
from traceback import extract_tb

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from .api import BaleAPI, BaleError
from .bot import Bot
from .coins import valid_coin_delivery
from .config import Config
from .education_access import valid_education_delivery
from .meetings import schedule_reminders, valid_delivery
from .models import Base, Conversation, Inbox, LoginThrottle, Outbox, Setting, utcnow
from .parent_meetings import parent_delivery_stage, schedule_parent_meetings


logger = logging.getLogger("schoolbot")
SCHEMA_VERSION = "2"


def error_location(error):
    for frame in reversed(extract_tb(error.__traceback__)):
        path = frame.filename.replace("\\", "/")
        if "/schoolbot/" in path:
            return f"{type(error).__name__} at {path.rsplit('/', 1)[-1]}:{frame.lineno}"
    return type(error).__name__


class Runner:
    def __init__(self, config, api=None, engine=None):
        self.config = config
        self.engine = engine or create_engine(config.database_url, pool_pre_ping=True)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.api = api or BaleAPI(config)
        self.bot = Bot(config, self.api)
        self.stopping = False
        self.lock_connection = None

    def initialize(self):
        if self.engine.dialect.name == "postgresql":
            self.lock_connection = self.engine.connect()
            acquired = self.lock_connection.scalar(text("SELECT pg_try_advisory_lock(1891614962)"))
            self.lock_connection.commit()
            if not acquired:
                self.lock_connection.close()
                self.lock_connection = None
                raise RuntimeError("یک نمونه دیگر ربات به این پایگاه داده متصل است.")
        with self.engine.begin() as connection:
            Base.metadata.create_all(connection)
            existing = {column["name"] for column in inspect(connection).get_columns("classrooms")}
            for column in ("instructor", "location", "schedule"):
                if column not in existing:
                    connection.exec_driver_sql(f"ALTER TABLE classrooms ADD COLUMN {column} VARCHAR(200)")
            student_columns = {column["name"] for column in inspect(connection).get_columns("students")}
            for column in ("father_phone", "mother_phone"):
                if column not in student_columns:
                    connection.exec_driver_sql(f"ALTER TABLE students ADD COLUMN {column} VARCHAR(20)")
            meeting_columns = {column["name"] for column in inspect(connection).get_columns("meetings")}
            if "revision" not in meeting_columns:
                connection.exec_driver_sql("ALTER TABLE meetings ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
            if "cancelled" not in meeting_columns:
                connection.exec_driver_sql("ALTER TABLE meetings ADD COLUMN cancelled BOOLEAN NOT NULL DEFAULT FALSE")
            with self.sessions(bind=connection) as session, session.begin():
                version = session.get(Setting, "schema_version")
                if version is None:
                    session.add(Setting(key="schema_version", value=SCHEMA_VERSION))
                elif version.value == "1":
                    version.value = SCHEMA_VERSION
                if session.get(Setting, "contacts") is None:
                    session.add(Setting(key="contacts", value=self.config.contact_text))

    def ingest(self, updates):
        if not isinstance(updates, list):
            raise BaleError(502)
        received = {}
        invalid = 0
        for update in updates:
            identifier = update.get("update_id") if isinstance(update, dict) else None
            if type(identifier) is not int or not 0 <= identifier < 2 ** 63 - 1:
                invalid += 1
            else:
                received.setdefault(identifier, update)
        if invalid:
            logger.warning("Ignored %s malformed incoming updates; payload withheld", invalid)
            if not received:
                raise BaleError(502)
        with self.sessions.begin() as session:
            cursor = session.get(Setting, "poll_offset")
            offset = int(cursor.value) if cursor else 0
            for identifier, update in received.items():
                if identifier >= offset and session.get(Inbox, identifier) is None:
                    session.add(Inbox(update_id=identifier, payload=update))
            if received:
                offset = max(offset, max(received) + 1)
            if cursor:
                cursor.value = str(offset)
            else:
                session.add(Setting(key="poll_offset", value=str(offset)))

    def offset(self):
        with self.sessions() as session:
            cursor = session.get(Setting, "poll_offset")
            return int(cursor.value) if cursor else 0

    def process_one(self):
        identifier = None
        try:
            with self.sessions.begin() as session:
                inbox = session.scalar(select(Inbox).where(
                    Inbox.status == "pending", Inbox.available_at <= utcnow(),
                ).order_by(Inbox.update_id).limit(1))
                if inbox is None:
                    return False
                identifier = inbox.update_id
                self.bot.handle(session, inbox.payload)
                inbox.status = "done"
                inbox.payload = {}
            return True
        except Exception as error:
            if identifier is None:
                raise
            logger.error("Update %s failed (%s); payload withheld", identifier, error_location(error))
            with self.sessions.begin() as session:
                inbox = session.get(Inbox, identifier)
                if not isinstance(error, BaleError) or error.code != 429:
                    inbox.attempts += 1
                delay = getattr(error, "retry_after", None) or min(300, 2 ** min(inbox.attempts, 8))
                inbox.available_at = utcnow() + timedelta(seconds=delay)
                if inbox.attempts >= 5:
                    inbox.status = "failed"
                    setting = session.get(Setting, "admin_chat_id")
                    admin_chat = int(setting.value) if setting else self.config.admin_id
                    self.bot.say(session, admin_chat,
                                 f"پردازش رویداد {identifier} پس از ۵ تلاش ناموفق متوقف شد. لاگ سرور را بررسی کنید؛ درخواست را دوباره بفرستید.")
            return True

    def deliver_one(self):
        with self.sessions.begin() as session:
            message = session.scalar(select(Outbox).where(
                Outbox.status == "pending", Outbox.available_at <= utcnow(),
            ).order_by(
                (Outbox.method != "answerCallbackQuery"),
                (Outbox.method == "copyMessage"), Outbox.id,
            ).limit(1))
            if message is None:
                return False
            key = message.dedupe_key or ""
            parent_stage = parent_delivery_stage(session, message, utcnow()) if key.startswith("pm:") else None
            if ((key.startswith("pm:") and parent_stage is None)
                    or (key.startswith("meeting:") and not valid_delivery(session, message, utcnow()))
                    or (key.startswith("coins:") and not valid_coin_delivery(session, message))
                    or (key.startswith(("edview:", "edfamily:", "edfeedback:"))
                        and not valid_education_delivery(session, message, self.config.admin_id))):
                message.status = "expired"
                message.payload = {}
                return True
            try:
                self.api.call(message.method, message.payload)
            except BaleError as error:
                message.attempts += 1
                message.last_error = str(error.code)
                permanent = error.code in (400, 401, 403, 404)
                if permanent or (message.attempts >= 10 and error.code != 429):
                    message.status = "failed"
                    logger.warning("Delivery %s failed permanently (%s)", message.id, error.code)
                else:
                    delay = error.retry_after or min(300, 2 ** min(message.attempts, 8))
                    message.available_at = utcnow() + timedelta(seconds=delay)
            else:
                if parent_stage is not None and message.method == "sendMessage":
                    parent_stage.delivered_at = utcnow()
                message.status = "sent"
                message.last_error = None
                message.payload = {}
        return True

    def prune(self):
        from sqlalchemy import delete, update

        cutoff = utcnow() - timedelta(days=30)
        with self.sessions.begin() as session:
            session.execute(delete(Outbox).where(Outbox.status.in_(("sent", "expired")), Outbox.created_at < cutoff))
            highest = session.scalar(select(func.max(Inbox.update_id)))
            if highest is not None:
                session.execute(delete(Inbox).where(Inbox.status == "done", Inbox.update_id < highest - 10000))
            session.execute(update(Conversation).where(
                Conversation.updated_at < utcnow() - timedelta(hours=2),
            ).values(data={}))
            session.execute(delete(LoginThrottle).where(LoginThrottle.window_start < cutoff))

    def stop(self, *_args):
        self.stopping = True

    def close(self):
        try:
            self.api.close()
        finally:
            try:
                if self.lock_connection is not None:
                    self.lock_connection.close()
            finally:
                self.engine.dispose()

    def run(self):
        self.initialize()
        logger.info("Database initialized; connecting to Bale")
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        method = "getMe"
        while not self.stopping:
            try:
                self.api.call(method)
                if method == "deleteWebhook":
                    break
                method = "deleteWebhook"
            except BaleError as error:
                if error.code in (400, 401, 403, 404):
                    raise RuntimeError(f"Bale startup failed: {method} ({error.code})") from None
                logger.warning("Bale startup unavailable: %s (%s); retrying", method, error.code)
                time.sleep(min(float(error.retry_after or 5), 300))
        ready = False
        next_prune = 0
        next_reminders = 0
        while not self.stopping:
            try:
                if self.lock_connection is not None:
                    self.lock_connection.execute(text("SELECT 1"))
                    self.lock_connection.commit()
                for _index in range(20):
                    if self.stopping or not self.process_one():
                        break
                for _index in range(10):
                    if self.stopping or not self.deliver_one():
                        break
                if self.stopping:
                    break
                updates = self.api.call("getUpdates", {"offset": self.offset(), "timeout": 2, "limit": 100})
                self.ingest(updates)
                if not ready:
                    logger.info("Bot started in long polling mode (getUpdates succeeded)")
                    ready = True
                if time.monotonic() >= next_reminders:
                    next_reminders = time.monotonic() + 30
                    try:
                        with self.sessions.begin() as session:
                            schedule_reminders(session)
                            schedule_parent_meetings(session)
                    except Exception as error:
                        logger.error("Meeting reminder job failed (%s)", error_location(error))
                if time.monotonic() >= next_prune:
                    next_prune = time.monotonic() + 3600
                    try:
                        self.prune()
                    except Exception as error:
                        logger.error("Database cleanup job failed (%s)", error_location(error))
            except BaleError as error:
                if error.code == 401:
                    raise RuntimeError("توکن ربات نامعتبر یا لغو شده است.") from None
                if error.code in (400, 403, 404):
                    raise RuntimeError(f"Bale polling rejected: getUpdates ({error.code})") from None
                if error.code == 409:
                    logger.warning("Bale getUpdates conflict (409); check for another instance using this token or an active webhook")
                else:
                    logger.warning("Bale getUpdates unavailable (%s); retrying", error.code)
                time.sleep(min(float(error.retry_after or 5), 300))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    runner = None
    try:
        runner = Runner(Config.from_env())
        runner.run()
    except (ValueError, RuntimeError) as error:
        logger.error("%s", error)
        raise SystemExit(1) from None
    except Exception as error:
        logger.error("Fatal error (%s); check database/server availability", error_location(error))
        raise SystemExit(1) from None
    finally:
        if runner:
            runner.close()
