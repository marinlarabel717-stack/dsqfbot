from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from telethon import TelegramClient, functions, errors
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    DeleteScheduledMessagesRequest,
    GetScheduledHistoryRequest,
    ImportChatInviteRequest,
    SendMessageRequest,
)

from .config import AppConfig
from .utils import normalize_link, slugify


INVITE_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)", re.IGNORECASE)
PUBLIC_RE = re.compile(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})/?$", re.IGNORECASE)


@dataclass(slots=True)
class LoginResult:
    need_password: bool
    label: str | None = None
    is_premium: bool = False


class TelethonManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.config.session_dir.mkdir(parents=True, exist_ok=True)
        self._supports_repeat = "schedule_repeat_period" in inspect.signature(SendMessageRequest.__init__).parameters

    def session_path(self, session_file: str) -> str:
        return str((self.config.session_dir / session_file).resolve())

    def build_client(self, session_file: str) -> TelegramClient:
        return TelegramClient(self.session_path(session_file), self.config.api_id, self.config.api_hash)

    def delete_session_files(self, session_file: str) -> None:
        base_path = Path(self.session_path(session_file))
        candidates = [base_path, base_path.with_suffix(".session"), base_path.with_suffix(".session-journal")]
        for item in candidates:
            try:
                if item.exists():
                    item.unlink()
            except OSError:
                continue

    async def begin_login(self, label: str, phone: str) -> tuple[str, str]:
        session_file = f"{slugify(label)}-{int(datetime.utcnow().timestamp())}"
        client = self.build_client(session_file)
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
            return session_file, sent.phone_code_hash
        finally:
            await client.disconnect()

    async def finish_login(
        self,
        session_file: str,
        phone: str,
        code: str,
        phone_code_hash: str,
        password: str | None = None,
    ) -> LoginResult:
        client = self.build_client(session_file)
        await client.connect()
        try:
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash, password=password)
            except errors.SessionPasswordNeededError:
                return LoginResult(need_password=True)
            me = await client.get_me()
            label = " ".join(part for part in [getattr(me, "first_name", ""), getattr(me, "last_name", "")] if part).strip()
            return LoginResult(need_password=False, label=label or phone, is_premium=bool(getattr(me, "premium", False)))
        finally:
            await client.disconnect()

    async def verify_session(self, session_row: dict[str, Any]) -> dict[str, Any]:
        client = self.build_client(session_row["session_file"])
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("账号掉线")
            me = await client.get_me()
            return {
                "label": " ".join(part for part in [getattr(me, "first_name", ""), getattr(me, "last_name", "")] if part).strip() or session_row["label"],
                "is_premium": bool(getattr(me, "premium", False)),
            }
        finally:
            await client.disconnect()

    async def list_groups(self, session_row: dict[str, Any]) -> list[dict[str, Any]]:
        client = self.build_client(session_row["session_file"])
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("账号掉线")
            dialogs = await client.get_dialogs(limit=200)
            items: list[dict[str, Any]] = []
            for dialog in dialogs:
                if not (dialog.is_group or dialog.is_channel):
                    continue
                entity = dialog.entity
                items.append(
                    {
                        "peer_id": int(getattr(entity, "id")),
                        "title": getattr(entity, "title", "") or getattr(entity, "first_name", "") or str(getattr(entity, "id")),
                        "username": getattr(entity, "username", None),
                        "link": f"https://t.me/{entity.username}" if getattr(entity, "username", None) else None,
                    }
                )
            return items
        finally:
            await client.disconnect()

    async def join_link(self, session_row: dict[str, Any], link: str) -> dict[str, Any]:
        link = normalize_link(link)
        client = self.build_client(session_row["session_file"])
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("账号掉线")
            invite_match = INVITE_RE.search(link)
            public_match = PUBLIC_RE.search(link)
            if invite_match:
                invite_hash = invite_match.group(1)
                try:
                    await client(CheckChatInviteRequest(invite_hash))
                    result = await client(ImportChatInviteRequest(invite_hash))
                    entity = None
                    chats = getattr(result, "chats", None) or []
                    if chats:
                        entity = chats[0]
                    if entity is None:
                        entity = await client.get_entity(link)
                    return {
                        "peer_id": int(getattr(entity, "id")),
                        "title": getattr(entity, "title", "") or str(getattr(entity, "id")),
                        "username": getattr(entity, "username", None),
                        "link": link,
                        "join_status": "joined",
                    }
                except errors.InviteRequestSentError:
                    preview = await client(CheckChatInviteRequest(invite_hash))
                    title = getattr(preview, "title", None) or "等待审批"
                    return {
                        "peer_id": 0,
                        "title": title,
                        "username": None,
                        "link": link,
                        "join_status": "awaiting_approval",
                    }
                except errors.UserAlreadyParticipantError:
                    entity = await client.get_entity(link)
                    return {
                        "peer_id": int(getattr(entity, "id")),
                        "title": getattr(entity, "title", "") or str(getattr(entity, "id")),
                        "username": getattr(entity, "username", None),
                        "link": link,
                        "join_status": "joined",
                    }
            if public_match:
                username = public_match.group(1)
                entity = await client.get_entity(username)
                try:
                    await client(JoinChannelRequest(entity))
                except errors.UserAlreadyParticipantError:
                    pass
                return {
                    "peer_id": int(getattr(entity, "id")),
                    "title": getattr(entity, "title", "") or str(getattr(entity, "id")),
                    "username": getattr(entity, "username", None),
                    "link": f"https://t.me/{username}",
                    "join_status": "joined",
                }
            if link.startswith("@"):
                entity = await client.get_entity(link)
                try:
                    await client(JoinChannelRequest(entity))
                except errors.UserAlreadyParticipantError:
                    pass
                return {
                    "peer_id": int(getattr(entity, "id")),
                    "title": getattr(entity, "title", "") or str(getattr(entity, "id")),
                    "username": getattr(entity, "username", None),
                    "link": link,
                    "join_status": "joined",
                }
            raise RuntimeError("无法识别群链接")
        finally:
            await client.disconnect()

    async def schedule_message(
        self,
        session_row: dict[str, Any],
        group_row: dict[str, Any],
        message_text: str,
        when: datetime,
    ) -> int:
        client = self.build_client(session_row["session_file"])
        await client.connect()
        try:
            entity = await self._resolve_entity(client, group_row)
            message = await client.send_message(entity, message_text, schedule=when)
            return int(message.id)
        finally:
            await client.disconnect()

    async def list_scheduled_messages(self, session_row: dict[str, Any], group_row: dict[str, Any]) -> list[dict[str, Any]]:
        client = self.build_client(session_row["session_file"])
        await client.connect()
        try:
            entity = await self._resolve_entity(client, group_row)
            result = await client(GetScheduledHistoryRequest(peer=entity, hash=0))
            items: list[dict[str, Any]] = []
            for message in getattr(result, "messages", []):
                date_value = getattr(message, "date", None)
                items.append(
                    {
                        "message_id": int(getattr(message, "id", 0)),
                        "text": getattr(message, "message", "") or "",
                        "schedule_at": date_value.isoformat() if date_value else None,
                    }
                )
            return items
        finally:
            await client.disconnect()

    async def delete_scheduled_message(self, session_row: dict[str, Any], group_row: dict[str, Any], message_id: int) -> None:
        client = self.build_client(session_row["session_file"])
        await client.connect()
        try:
            entity = await self._resolve_entity(client, group_row)
            await client(DeleteScheduledMessagesRequest(peer=entity, id=[message_id]))
        finally:
            await client.disconnect()

    async def detect_group_status(self, session_row: dict[str, Any], group_row: dict[str, Any]) -> dict[str, str]:
        client = self.build_client(session_row["session_file"])
        await client.connect()
        try:
            try:
                entity = await self._resolve_entity(client, group_row)
            except errors.UserNotParticipantError:
                return {"join_status": "not_joined", "speak_status": "未加入群", "last_error": "未加入群"}
            permissions = await client.get_permissions(entity, "me")
            if getattr(permissions, "is_banned", False):
                return {"join_status": "joined", "speak_status": "禁言", "last_error": "禁言"}
            if getattr(permissions, "has_left", False):
                return {"join_status": "left", "speak_status": "未加入群", "last_error": "账号已离开群"}
            if getattr(permissions, "send_messages", None) is False:
                return {"join_status": "joined", "speak_status": "无发言权限", "last_error": "无发言权限"}
            return {"join_status": "joined", "speak_status": "正常可发", "last_error": ""}
        finally:
            await client.disconnect()

    async def _resolve_entity(self, client: TelegramClient, group_row: dict[str, Any]):
        if group_row.get("username"):
            return await client.get_entity(group_row["username"])
        if int(group_row.get("peer_id") or 0):
            return await client.get_entity(int(group_row["peer_id"]))
        if group_row.get("link"):
            return await client.get_entity(group_row["link"])
        raise errors.UserNotParticipantError(request=None)

    def next_daily_run(self, when: datetime) -> datetime:
        return when + timedelta(days=1)

    @property
    def supports_repeat(self) -> bool:
        return self._supports_repeat

    @staticmethod
    def describe_error(exc: Exception) -> str:
        mapping = {
            "ChatWriteForbiddenError": "无发言权限",
            "UserBannedInChannelError": "禁言",
            "UserNotParticipantError": "未加入群",
            "InviteRequestSentError": "等待审批",
            "ChannelPrivateError": "群不可访问",
            "AuthKeyUnregisteredError": "账号掉线",
            "SessionRevokedError": "账号掉线",
        }
        name = exc.__class__.__name__
        if name in mapping:
            return mapping[name]
        message = str(exc).strip() or name
        if "A wait of" in message:
            return f"风控等待 {TelethonManager.extract_wait_seconds(exc)} 秒"
        return message

    @staticmethod
    def extract_wait_seconds(exc: Exception) -> int:
        seconds = int(getattr(exc, "seconds", 0) or 0)
        if seconds > 0:
            return seconds
        match = re.search(r"A wait of (\d+) seconds", str(exc))
        if match:
            return int(match.group(1))
        return 60
