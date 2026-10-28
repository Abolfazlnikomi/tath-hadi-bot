import base64
import json
import time
from urllib.parse import quote, unquote, urlsplit

import httpx


class BaleError(Exception):
    def __init__(self, code, retry_after=None):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Bale API error {code}")


class BaleAPI:
    def __init__(self, config):
        self.config = config
        self.base = f"https://tapi.bale.ai/bot{config.token}/"
        self.client = httpx.Client(timeout=45, follow_redirects=False)
        self.next_request = 0.0

    def close(self):
        self.client.close()

    def call(self, method, payload=None):
        delay = self.next_request - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        payload = dict(payload or {})
        file_data = payload.pop("_file", None)
        try:
            if file_data:
                response = self.client.post(
                    self.base + method,
                    data={key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                          for key, value in payload.items()},
                    files={"document": (
                        file_data["name"], base64.b64decode(file_data["content"]),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )},
                )
            else:
                response = self.client.post(self.base + method, json=payload)
            try:
                result = response.json()
            except ValueError:
                raise BaleError(response.status_code if response.status_code >= 400 else 502) from None
            if not isinstance(result, dict):
                raise BaleError(502)
            if not result.get("ok"):
                retry_after = result.get("parameters", {}).get("retry_after")
                if retry_after:
                    self.next_request = time.monotonic() + float(retry_after)
                raise BaleError(result.get("error_code", response.status_code), retry_after)
            return result.get("result")
        except httpx.HTTPError:
            raise BaleError(503) from None
        finally:
            self.next_request = max(self.next_request, time.monotonic() + self.config.send_interval)

    def download(self, document):
        if document.get("file_size", 0) > self.config.max_file_bytes:
            raise ValueError("حجم فایل بیش از حد مجاز است.")
        if not document.get("file_name", "").lower().endswith(".xlsx"):
            raise ValueError("فقط فایل xlsx مجاز است.")
        info = self.call("getFile", {"file_id": document["file_id"]})
        path = info.get("file_path", "") if isinstance(info, dict) else ""
        url = self.download_url(path)
        try:
            with self.client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise BaleError(response.status_code)
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > self.config.max_file_bytes:
                        raise ValueError("حجم فایل بیش از حد مجاز است.")
                return bytes(content)
        except httpx.HTTPError:
            raise BaleError(503) from None

    def download_url(self, path):
        if not isinstance(path, str) or not path or any(character.isspace() for character in path):
            raise ValueError("مسیر دانلود فایل معتبر نیست.")
        try:
            parsed = urlsplit(path)
            absolute = path.lower().startswith("https://")
            if absolute:
                if (parsed.hostname != "tapi.bale.ai" or parsed.port not in (None, 443)
                        or parsed.username is not None or parsed.password is not None or parsed.fragment):
                    raise ValueError
                decoded = unquote(parsed.path)
            else:
                if path.startswith("//"):
                    raise ValueError
                decoded = unquote(path)
            if ("\\" in decoded or ".." in decoded.split("/")
                    or any(ord(character) < 32 for character in decoded)):
                raise ValueError
            if absolute:
                return path
            if "://" in path or "?" in path or "#" in path:
                raise ValueError
            return f"https://tapi.bale.ai/file/bot{self.config.token}/{quote(path.lstrip('/'), safe='/')}"
        except ValueError:
            raise ValueError("مسیر دانلود فایل معتبر نیست.") from None
