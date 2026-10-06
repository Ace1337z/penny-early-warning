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

    def send(self, text: str, chat_id: Optional[str] = None, disable_preview: bool = True,
             markup: Optional[dict] = None, *, html: bool = False,
             silent: bool = False) -> Optional[int]:
        """Send a message, splitting long text. Returns the last message id."""
        chat = chat_id or self.chat_id
        if not chat:
            raise TelegramError("telegram: TELEGRAM_CHAT_ID not set")
        msg_id = None
        chunks = split_message(text)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat,
                "text": chunk,
                "disable_web_page_preview": disable_preview,
                "disable_notification": silent,
            }
            if html:
                payload["parse_mode"] = "HTML"
            # Attach the keyboard to the final chunk only.
            if markup is not None and index == len(chunks) - 1:
                payload["reply_markup"] = markup
            result = self._call("sendMessage", payload)
            if isinstance(result, dict):
                msg_id = result.get("message_id", msg_id)
        return msg_id

    def edit(self, msg_id: int, text: str, chat_id: Optional[str] = None,
             markup: Optional[dict] = None, *, html: bool = False) -> bool:
        chat = chat_id or self.chat_id
        if not chat or not msg_id:
            return False
        body = "\n".join(split_message(text))
        key = (body, )
        prev = self._last_edit.get(msg_id)
        if prev and prev[0] == body:
            return True
        payload: dict[str, Any] = {
            "chat_id": chat,
            "message_id": msg_id,
            "text": body[:4096],
            "disable_web_page_preview": True,
        }
        if html:
            payload["parse_mode"] = "HTML"
        if markup is not None:
            payload["reply_markup"] = markup
        try:
            self._call("editMessageText", payload)
            self._last_edit[msg_id] = (body, time.time())
            return True
        except TelegramError as exc:
            if "message is not modified" in str(exc).lower():
                return True
            log.warning("telegram edit failed: %s", exc)
            return False

    def answer_callback(self, callback_id: str, text: str = "",
                        show_alert: bool = False) -> bool:
        """Acknowledge a button tap so the client stops the loading spinner."""
        if not callback_id:
            return False
        try:
            self._call("answerCallbackQuery",
                       {"callback_query_id": callback_id, "text": text[:200] or None,
                        "show_alert": show_alert})
            return True
        except TelegramError as exc:
            log.debug("answerCallbackQuery failed: %s", exc)
            return False

    def send_chat_action(self, action: str = "typing",
                         chat_id: Optional[str] = None) -> bool:
        """Show 'typing...' so a slow command feels responsive."""
        chat = chat_id or self.chat_id
        if not chat:
            return False
        try:
            self._call("sendChatAction", {"chat_id": chat, "action": action})
            return True
        except TelegramError:
            return False

    def set_commands(self, commands: Iterable[tuple[str, str]]) -> bool:
        """Publish the command menu shown in Telegram's UI."""
        try:
            self._call("setMyCommands", {
                "commands": [{"command": c.lstrip("/"), "description": d}
                             for c, d in commands][:100]})
            return True
        except TelegramError as exc:
            log.debug("setMyCommands failed: %s", exc)
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
    def get_updates(self, offset: int = 0, timeout: int = 25,
                    allowed_updates: Optional[list[str]] = None) -> list[dict]:
        if not self.token:
            return []
        payload: dict[str, Any] = {"offset": offset, "timeout": timeout}
        payload["allowed_updates"] = allowed_updates or ["message", "callback_query"]
        try:
            result = self._call("getUpdates", payload, timeout=timeout + 10)
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
        self.callbacks: list[dict] = []
        self.keyboards: list[Optional[dict]] = []
        self.updates: list[dict] = []
        self.actions: list[str] = []
        self.menu: list[tuple[str, str]] = []

    def send(self, text: str, chat_id: Optional[str] = None, disable_preview: bool = True,
             markup: Optional[dict] = None, *, html: bool = False,
             silent: bool = False) -> int:
        self._next_id += 1
        self.messages.append({"id": self._next_id, "text": text, "markup": markup})
        self.outbox.append(text)
        self.keyboards.append(markup)
        return self._next_id

    def edit(self, msg_id: int, text: str, chat_id: Optional[str] = None,
             markup: Optional[dict] = None, *, html: bool = False) -> bool:
        self.edits.append({"id": msg_id, "text": text, "markup": markup})
        self.keyboards.append(markup)
        for m in self.messages:
            if m["id"] == msg_id:
                m["text"] = text
                m["markup"] = markup
        return True

    def send_document(self, path, caption: str = "", chat_id: Optional[str] = None) -> bool:
        self.documents.append(str(path))
        return True

    def delete(self, msg_id: int, chat_id: Optional[str] = None) -> bool:
        self.messages = [m for m in self.messages if m["id"] != msg_id]
        return True

    def get_updates(self, offset: int = 0, timeout: int = 25,
                    allowed_updates: Optional[list[str]] = None) -> list[dict]:
        pending = [u for u in self.updates if int(u.get("update_id", 0)) >= offset]
        self.updates = [u for u in self.updates if int(u.get("update_id", 0)) < offset]
        return pending

    def answer_callback(self, callback_id: str, text: str = "",
                        show_alert: bool = False) -> bool:
        self.callbacks.append({"id": callback_id, "text": text, "alert": show_alert})
        return True

    def send_chat_action(self, action: str = "typing",
                         chat_id: Optional[str] = None) -> bool:
        self.actions.append(action)
        return True

    def set_commands(self, commands) -> bool:
        self.menu = list(commands)
        return True

    def health(self) -> tuple[bool, str]:
        return True, "fake telegram"

    def text_of(self, msg_id: int) -> str:
        for m in self.messages:
            if m["id"] == msg_id:
                return m["text"]
        return ""
