import os
import unittest
from unittest.mock import patch

from schoolbot.config import Config


class ConfigTests(unittest.TestCase):
    def config(self, **variables):
        with patch.dict(os.environ, {"BALE_BOT_TOKEN": "test-token", **variables}, clear=True):
            with patch("schoolbot.config.load_dotenv"):
                return Config.from_env()

    def test_default_connection_is_backwards_compatible(self):
        url = "postgresql+psycopg://user:password@localhost/db"
        self.assertEqual(self.config(DATABASE_URL=url).database_url, url)

    def test_local_and_main_selection(self):
        for target in ("local", "main"):
            config = self.config(DATABASE_TARGET=target,
                                 LOCAL_DATABASE_URL="postgresql://user:password@localhost/local",
                                 MAIN_DATABASE_URL="postgresql://user:password@remote/main?sslmode=require")
            self.assertIn("localhost/local" if target == "local" else "remote/main", config.database_url)
            self.assertTrue(config.database_url.startswith("postgresql+psycopg://"))
            self.assertNotIn("password", repr(config))

    def test_missing_target_url_never_falls_back(self):
        with self.assertRaisesRegex(ValueError, "MAIN_DATABASE_URL"):
            self.config(DATABASE_TARGET="main", DATABASE_URL="postgresql://localhost/local")

    def test_invalid_target(self):
        with self.assertRaisesRegex(ValueError, "DATABASE_TARGET"):
            self.config(DATABASE_TARGET="invalid")
