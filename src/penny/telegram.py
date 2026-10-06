"""Telegram delivery: alerts, edits, documents, command polling (5.3 / 6.12)."""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Optional

from .util import scrub

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE = 3900


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    """Real Telegram Bot API client. Commands are accepted only from the configured chat."""

    def __init__(self, cfg, secrets: Optional[Iterable[str]] = None):
        self.cfg = cfg
        self._secrets = list(secrets or [])
        self._session = None
        self._last_edit: dict[int, tuple[str, float]] = {}

    @property
    def token(self) -> str:
        return str(self.cfg.raw("TELEGRAM_TOKEN") or "")

    @property
    def chat_id(self) -> str:
        return str(self.cfg.raw("TELEGRAM_CHAT_ID") or "")

    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _url(self, method: str) -> str:
        return API.format(token=self.token, method=method)

    def _call(self, method: str, payload: dict, timeout: float = 20.0) -> Any:
        if not self.token:
            raise TelegramError("telegram: TELEGRAM_TOKEN not set")
        import requests
        if self._session is None:
            self._session = requests.Session()
        for attempt in range(3):
            try:
                resp = self._session.post(self._url(method), json=payload, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    raise TelegramError(scrub(str(exc), self._secrets)) from exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After") or 3)
                time.sleep(wait)
                continue
            try:
                data = resp.json()
            except ValueError as exc:
                raise TelegramError("telegram: invalid JSON response") from exc
            if not data.get("ok"):
                desc = scrub(str(data.get("description") or "unknown error"), self._secrets)
                raise TelegramError(f"telegram: {desc}")
            return data.get("result")
        raise TelegramError("telegram: send failed after retries")

    # -- sending ------------------------------------------------------------
    def delete(self, msg_id: int, chat_id: Optional[str] = None) -> bool:
        """Delete a message (used to remove secret values from the chat)."""
        chat = chat_id or self.chat_id
        if not chat or not msg_id:
            return False
        try:
            self._call("deleteMessage", {"chat_id": chat, "message_id": msg_id})
            return True
        except TelegramError as exc:
            log.debug("telegram delete failed: %s", exc)
            return False

    def send(self, text: str, chat_id: Optional[str] = None, disable_preview: bool = True) -> Optional[int]:
        """Send a message, splitting long text. Returns the last message id."""
        chat = chat_id or self.chat_id
        if not chat:
            raise TelegramError("telegram: TELEGRAM_CHAT_ID not set")
        msg_id = None
        for chunk in split_message(text):
            result = self._call("sendMessage", {
                "chat_id": chat,
                "text": chunk,
                "disable_web_page_preview": disable_preview,
            })
            if isinstance(result, dict):
                msg_id = result.get("message_id", msg_id)
        return msg_id

    def edit(self, msg_id: int, text: str, chat_id: Optional[str] = None) -> bool:
        chat = chat_id or self.chat_id
        if not chat or not msg_id:
            return False
        body = "\n".join(split_message(text))
        # Telegram rejects an edit that changes nothing; skip duplicates.
        key = (body, time.time())
        prev = self._last_edit.get(msg_id)
        if prev and prev[0] == body:
            return True
        try:
            self._call("editMessageText", {
                "chat_id": chat,
                "message_id": msg_id,
                "text": body[:4096],
                "disable_web_page_preview": True,
            })
            self._last_edit[msg_id] = key
            return True
        except TelegramError as exc:
            if "message is not modified" in str(exc).lower():
                return True
            log.warning("telegram edit failed: %s", exc)
            return False

    def send_document(self, path, caption: str = "", chat_id: Optional[str] = None) -> bool:
        chat = chat_id or self.chat_id
        if not self.token or not chat:
            return False
        import requests
        if self._session is None:
            self._session = requests.Session()
        try:
            with open(path, "rb") as fh:
                resp = self._session.post(
                    self._url("sendDocument"),
                    data={"chat_id": chat, "caption": caption[:1024]},
                    files={"document": fh},
                    timeout=120.0,
                )
            return resp.status_code == 200 and resp.json().get("ok", False)
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram document send failed: %s", scrub(str(exc), self._secrets))
            return False

    # -- polling ------------------------------------------------------------
    def get_updates(self, offset: int = 0, timeout: int = 25) -> list[dict]:
        if not self.token:
            return []
        try:
            result = self._call("getUpdates", {"offset": offset, "timeout": timeout},
                                timeout=timeout + 10)
        except TelegramError as exc:
            log.debug("telegram getUpdates failed: %s", exc)
            return []
        return result if isinstance(result, list) else []

    def detect_chat_id(self, attempts: int = 30, timeout: int = 2) -> Optional[str]:
        """Helper for the installer: read the chat id after the user messages the bot."""
        offset = 0
        for _ in range(attempts):
            for upd in self.get_updates(offset=offset, timeout=timeout):
                offset = max(offset, upd.get("update_id", 0) + 1)
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat = (msg.get("chat") or {}).get("id")
                if chat is not None:
                    return str(chat)
        return None

    def health(self) -> tuple[bool, str]:
        if not self.token:
            return False, "TELEGRAM_TOKEN not set"
        try:
            result = self._call("getMe", {})
            username = (result or {}).get("username", "?")
            if not self.chat_id:
                return False, f"bot @{username} ok but TELEGRAM_CHAT_ID not set"
            self._call("sendMessage", {"chat_id": self.chat_id, "text": "penny doctor: PASS",
                                       "disable_web_page_preview": True})
            return True, f"bot @{username} reachable, chat ok"
        except Exception as exc:  # noqa: BLE001
            return False, scrub(str(exc), self._secrets)


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split long messages on line boundaries, preferring paragraph breaks."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


class FakeTelegram:
    """Records messages for offline simulation and tests (10.1)."""

    def __init__(self, cfg=None):
        self.cfg = cfg
        self.messages: list[dict] = []
        self._next_id = 1000
        self.edits: list[dict] = []
        self.documents: list[str] = []
        self.outbox: list[str] = []
        self.commands: list[str] = []

    def send(self, text: str, chat_id: Optional[str] = None, disable_preview: bool = True) -> int:
        self._next_id += 1
        self.messages.append({"id": self._next_id, "text": text})
        self.outbox.append(text)
        return self._next_id

    def edit(self, msg_id: int, text: str, chat_id: Optional[str] = None) -> bool:
        self.edits.append({"id": msg_id, "text": text})
        for m in self.messages:
            if m["id"] == msg_id:
                m["text"] = text
        return True

    def send_document(self, path, caption: str = "", chat_id: Optional[str] = None) -> bool:
        self.documents.append(str(path))
        return True

    def delete(self, msg_id: int, chat_id: Optional[str] = None) -> bool:
        self.messages = [m for m in self.messages if m["id"] != msg_id]
        return True

    def get_updates(self, offset: int = 0, timeout: int = 25) -> list[dict]:
        return []

    def health(self) -> tuple[bool, str]:
        return True, "fake telegram"

    def text_of(self, msg_id: int) -> str:
        for m in self.messages:
            if m["id"] == msg_id:
                return m["text"]
        return ""
