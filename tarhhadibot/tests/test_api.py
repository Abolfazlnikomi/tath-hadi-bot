import base64
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx

from schoolbot.api import BaleAPI, BaleError
from schoolbot.config import Config


class APITests(unittest.TestCase):
    def setUp(self):
        self.config = Config("secret-test-token", "unused", send_interval=0)
        self.api = BaleAPI(self.config)
        self.api.client.close()

    def tearDown(self):
        self.api.close()

    def transport(self, handler):
        self.api.client = httpx.Client(transport=httpx.MockTransport(handler))

    def test_json_request(self):
        def handle(request):
            self.assertEqual(request.url.path, "/botsecret-test-token/sendMessage")
            self.assertEqual(request.headers["content-type"], "application/json")
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        self.transport(handle)
        self.assertEqual(self.api.call("sendMessage", {"chat_id": 1, "text": "سلام"}), {"message_id": 1})

    def test_document_multipart(self):
        def handle(request):
            self.assertIn("multipart/form-data", request.headers["content-type"])
            self.assertIn(b"file.xlsx", request.content)
            self.assertIn(b"excel-bytes", request.content)
            return httpx.Response(200, json={"ok": True, "result": True})

        self.transport(handle)
        self.api.call("sendDocument", {"chat_id": 1, "_file": {
            "name": "file.xlsx", "content": base64.b64encode(b"excel-bytes").decode(),
        }})

    def test_rate_limit(self):
        self.transport(lambda request: httpx.Response(429, json={
            "ok": False, "error_code": 429, "parameters": {"retry_after": 12},
        }))
        with self.assertRaises(BaleError) as raised:
            self.api.call("sendMessage", {})
        self.assertEqual(raised.exception.retry_after, 12)
        self.assertNotIn(self.config.token, str(raised.exception))

    def test_network_failure_sanitizes_token(self):
        def handle(request):
            raise httpx.ConnectError("secret-test-token", request=request)

        self.transport(handle)
        with self.assertRaises(BaleError) as raised:
            self.api.call("getUpdates")
        self.assertEqual(raised.exception.code, 503)
        self.assertNotIn(self.config.token, str(raised.exception))

    def test_non_json_response(self):
        self.transport(lambda request: httpx.Response(502, text="gateway down"))
        with self.assertRaises(BaleError) as raised:
            self.api.call("getUpdates")
        self.assertEqual(raised.exception.code, 502)

    def test_non_object_json_response(self):
        self.transport(lambda request: httpx.Response(200, json=[]))
        with self.assertRaises(BaleError) as raised:
            self.api.call("getUpdates")
        self.assertEqual(raised.exception.code, 502)

    def test_download_size_enforced_on_stream(self):
        self.api.config = replace(self.config, max_file_bytes=3)

        def handle(request):
            if "getFile" in request.url.path:
                return httpx.Response(200, json={"ok": True, "result": {"file_path": "files/test.xlsx"}})
            self.assertEqual(request.url.host, "tapi.bale.ai")
            return httpx.Response(200, content=b"1234")

        self.transport(handle)
        with self.assertRaises(ValueError):
            self.api.download({"file_name": "test.xlsx", "file_id": "id"})

    def test_download_valid(self):
        def handle(request):
            if "getFile" in request.url.path:
                return httpx.Response(200, json={"ok": True, "result": {"file_path": "files/test.xlsx"}})
            self.assertEqual(request.url.path, "/file/botsecret-test-token/files/test.xlsx")
            return httpx.Response(200, content=b"excel")

        self.transport(handle)
        self.assertEqual(self.api.download({"file_name": "test.xlsx", "file_id": "id"}), b"excel")

    def test_download_rejects_foreign_url(self):
        self.transport(lambda request: httpx.Response(200, json={
            "ok": True, "result": {"file_path": "https://evil.invalid/file"},
        }))
        with self.assertRaises(ValueError):
            self.api.download({"file_name": "test.xlsx", "file_id": "id"})

    def test_download_rejects_extension_and_size_before_request(self):
        for document in ({"file_name": "test.xls"}, {"file_name": "test.xlsx", "file_size": 999999999}):
            with self.assertRaises(ValueError):
                self.api.download(document)

    def test_config_no_token_leak(self):
        self.assertNotIn(self.config.token, repr(self.config))

    def test_config_requires_postgres_and_token(self):
        with patch("schoolbot.config.load_dotenv"), patch.dict("os.environ", {
            "BALE_BOT_TOKEN": "test-token", "DATABASE_URL": "sqlite:///test.db",
        }, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()


if __name__ == "__main__":
    unittest.main()
