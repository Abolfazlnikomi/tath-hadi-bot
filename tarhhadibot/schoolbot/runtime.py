import logging
import signal
import time
from datetime import timedelta

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from .api import BaleAPI, BaleError
from .bot import Bot
from .config import Config
from .models import Base, Conversation, Inbox, LoginThrottle, Outbox, Setting, utcnow


logger = logging.getLogger("schoolbot")


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
        Base.metadata.create_all(self.engine)
        with self.sessions.begin() as session:
            if session.get(Setting, "schema_version") is None:
                session.add(Setting(key="schema_version", value="1"))
            if session.get(Setting, "contacts") is None:
                session.add(Setting(key="contacts", value=self.config.contact_text))

    def ingest(self, updates):
        with self.sessions.begin() as session:
            cursor = session.get(Setting, "poll_offset")
            offset = int(cursor.value) if cursor else 0
            for update in updates:
                identifier = update["update_id"]
                if session.get(Inbox, identifier) is None:
                    session.add(Inbox(update_id=identifier, payload=update))
                offset = max(offset, identifier + 1)
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
                inbox = session.scalar(select(Inbox).where(Inbox.status == "pending").order_by(Inbox.update_id).limit(1))
                if inbox is None:
                    return False
                if inbox.available_at.replace(tzinfo=utcnow().tzinfo) > utcnow():
                    return False
                identifier = inbox.update_id
                self.bot.handle(session, inbox.payload)
                inbox.status = "done"
                inbox.payload = {}
            return True
        except Exception as error:
            if identifier is None:
                raise
            logger.error("Update %s failed (%s); payload withheld", identifier, type(error).__name__)
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
                message.status = "sent"
                message.last_error = None
                message.payload = {}
        return True

    def prune(self):
        from sqlalchemy import delete, update

        cutoff = utcnow() - timedelta(days=30)
        with self.sessions.begin() as session:
            session.execute(delete(Outbox).where(Outbox.status == "sent", Outbox.created_at < cutoff))
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
        self.api.close()
        if self.lock_connection is not None:
            try:
                self.lock_connection.execute(text("SELECT pg_advisory_unlock(1891614962)"))
                self.lock_connection.commit()
            finally:
                self.lock_connection.close()
        self.engine.dispose()

    def run(self):
        self.initialize()
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        while not self.stopping:
            try:
                self.api.call("getMe")
                self.api.call("deleteWebhook")
                break
            except BaleError as error:
                if error.code in (400, 401, 403, 404):
                    raise RuntimeError("اتصال اولیه بله رد شد؛ توکن و دسترسی را بررسی کنید.") from None
                logger.warning("Bale startup unavailable (%s); retrying", error.code)
                time.sleep(min(float(error.retry_after or 5), 300))
        logger.info("Bot started in long polling mode")
        next_prune = 0
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
                updates = self.api.call("getUpdates", {"offset": self.offset(), "timeout": 2, "limit": 100})
                self.ingest(updates or [])
                if time.monotonic() >= next_prune:
                    self.prune()
                    next_prune = time.monotonic() + 3600
            except BaleError as error:
                if error.code == 401:
                    raise RuntimeError("توکن ربات نامعتبر یا لغو شده است.") from None
                logger.warning("Bale unavailable (%s); retrying", error.code)
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
        logger.error("Fatal error (%s); check database/server availability", type(error).__name__)
        raise SystemExit(1) from None
    finally:
        if runner:
            runner.close()
