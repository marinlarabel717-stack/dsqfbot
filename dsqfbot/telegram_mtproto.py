from __future__ import annotations

import asyncio
import os
import random
import re
import struct
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telethon import TelegramClient, errors, functions
from telethon.tl import alltlobjects, patched as patched_types, types
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    DeleteScheduledMessagesRequest,
    GetScheduledHistoryRequest,
    ImportChatInviteRequest,
    SendMessageRequest,
)
from telethon.tl.tlobject import TLRequest

from .config import AppConfig
from .utils import normalize_link, slugify


INVITE_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)", re.IGNORECASE)
PUBLIC_RE = re.compile(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})/?$", re.IGNORECASE)
TELEGRAM_DAILY_REPEAT_PERIOD = 24 * 60 * 60
CURRENT_MESSAGE_CONSTRUCTOR_ID = 0x3AE56482
CURRENT_REPEAT_SEND_MESSAGE_CONSTRUCTOR_ID = 0x545CD15A


class SendMessageWithRepeatRequest(TLRequest):
    CONSTRUCTOR_ID = CURRENT_REPEAT_SEND_MESSAGE_CONSTRUCTOR_ID
    SUBCLASS_OF_ID = SendMessageRequest.SUBCLASS_OF_ID

    def __init__(
        self,
        peer: Any,
        message: str,
        no_webpage: bool | None = None,
        silent: bool | None = None,
        background: bool | None = None,
        clear_draft: bool | None = None,
        noforwards: bool | None = None,
        update_stickersets_order: bool | None = None,
        invert_media: bool | None = None,
        allow_paid_floodskip: bool | None = None,
        reply_to: Any | None = None,
        random_id: int | None = None,
        reply_markup: Any | None = None,
        entities: list[Any] | None = None,
        schedule_date: datetime | None = None,
        schedule_repeat_period: int | None = None,
        send_as: Any | None = None,
        quick_reply_shortcut: Any | None = None,
        effect: int | None = None,
        allow_paid_stars: int | None = None,
        suggested_post: Any | None = None,
    ) -> None:
        self.peer = peer
        self.message = message
        self.no_webpage = no_webpage
        self.silent = silent
        self.background = background
        self.clear_draft = clear_draft
        self.noforwards = noforwards
        self.update_stickersets_order = update_stickersets_order
        self.invert_media = invert_media
        self.allow_paid_floodskip = allow_paid_floodskip
        self.reply_to = reply_to
        self.random_id = random_id if random_id is not None else int.from_bytes(os.urandom(8), "big", signed=True)
        self.reply_markup = reply_markup
        self.entities = entities
        self.schedule_date = schedule_date
        self.schedule_repeat_period = schedule_repeat_period if schedule_repeat_period and schedule_repeat_period > 0 else None
        self.send_as = send_as
        self.quick_reply_shortcut = quick_reply_shortcut
        self.effect = effect
        self.allow_paid_stars = allow_paid_stars
        self.suggested_post = suggested_post

    async def resolve(self, client, utils) -> None:
        self.peer = utils.get_input_peer(await client.get_input_entity(self.peer))
        if self.send_as:
            self.send_as = utils.get_input_peer(await client.get_input_entity(self.send_as))

    def _bytes(self) -> bytes:
        flags = (
            (0 if self.no_webpage is None or self.no_webpage is False else 2)
            | (0 if self.silent is None or self.silent is False else 32)
            | (0 if self.background is None or self.background is False else 64)
            | (0 if self.clear_draft is None or self.clear_draft is False else 128)
            | (0 if self.noforwards is None or self.noforwards is False else 16384)
            | (0 if self.update_stickersets_order is None or self.update_stickersets_order is False else 32768)
            | (0 if self.invert_media is None or self.invert_media is False else 65536)
            | (0 if self.allow_paid_floodskip is None or self.allow_paid_floodskip is False else 524288)
            | (0 if self.reply_to is None or self.reply_to is False else 1)
            | (0 if self.reply_markup is None or self.reply_markup is False else 4)
            | (0 if self.entities is None or self.entities is False else 8)
            | (0 if self.schedule_date is None or self.schedule_date is False else 1024)
            | (0 if self.send_as is None or self.send_as is False else 8192)
            | (0 if self.quick_reply_shortcut is None or self.quick_reply_shortcut is False else 131072)
            | (0 if self.effect is None or self.effect is False else 262144)
            | (0 if self.allow_paid_stars is None or self.allow_paid_stars is False else 2097152)
            | (0 if self.suggested_post is None or self.suggested_post is False else 4194304)
            | (0 if self.schedule_repeat_period is None or self.schedule_repeat_period is False else 16777216)
        )
        return b"".join(
            (
                struct.pack("<I", self.CONSTRUCTOR_ID),
                struct.pack("<I", flags),
                self.peer._bytes(),
                b"" if self.reply_to is None or self.reply_to is False else self.reply_to._bytes(),
                self.serialize_bytes(self.message),
                struct.pack("<q", self.random_id),
                b"" if self.reply_markup is None or self.reply_markup is False else self.reply_markup._bytes(),
                b""
                if self.entities is None or self.entities is False
                else b"".join((b"\x15\xc4\xb5\x1c", struct.pack("<i", len(self.entities)), b"".join(x._bytes() for x in self.entities))),
                b"" if self.schedule_date is None or self.schedule_date is False else self.serialize_datetime(self.schedule_date),
                b"" if self.schedule_repeat_period is None or self.schedule_repeat_period is False else struct.pack("<i", int(self.schedule_repeat_period)),
                b"" if self.send_as is None or self.send_as is False else self.send_as._bytes(),
                b"" if self.quick_reply_shortcut is None or self.quick_reply_shortcut is False else self.quick_reply_shortcut._bytes(),
                b"" if self.effect is None or self.effect is False else struct.pack("<q", self.effect),
                b"" if self.allow_paid_stars is None or self.allow_paid_stars is False else struct.pack("<q", self.allow_paid_stars),
                b"" if self.suggested_post is None or self.suggested_post is False else self.suggested_post._bytes(),
            )
        )


def _patched_message_from_reader(cls, reader):
    flags = reader.read_int()

    _out = bool(flags & 2)
    _mentioned = bool(flags & 16)
    _media_unread = bool(flags & 32)
    _silent = bool(flags & 8192)
    _post = bool(flags & 16384)
    _from_scheduled = bool(flags & 262144)
    _legacy = bool(flags & 524288)
    _edit_hide = bool(flags & 2097152)
    _pinned = bool(flags & 16777216)
    _noforwards = bool(flags & 67108864)
    _invert_media = bool(flags & 134217728)
    flags2 = reader.read_int()

    _offline = bool(flags2 & 2)
    _video_processing_pending = bool(flags2 & 16)
    _paid_suggested_post_stars = bool(flags2 & 256)
    _paid_suggested_post_ton = bool(flags2 & 512)
    _id = reader.read_int()
    _from_id = reader.tgread_object() if flags & 256 else None
    _from_boosts_applied = reader.read_int() if flags & 536870912 else None
    _from_rank = reader.tgread_string() if flags2 & 4096 else None
    _peer_id = reader.tgread_object()
    _saved_peer_id = reader.tgread_object() if flags & 268435456 else None
    _fwd_from = reader.tgread_object() if flags & 4 else None
    _via_bot_id = reader.read_long() if flags & 2048 else None
    _via_business_bot_id = reader.read_long() if flags2 & 1 else None
    _guestchat_via_from = reader.tgread_object() if flags2 & 524288 else None
    _reply_to = reader.tgread_object() if flags & 8 else None
    _date = reader.tgread_date()
    _message = reader.tgread_string()
    _media = reader.tgread_object() if flags & 512 else None
    _reply_markup = reader.tgread_object() if flags & 64 else None
    if flags & 128:
        reader.read_int()
        _entities = [reader.tgread_object() for _ in range(reader.read_int())]
    else:
        _entities = None
    _views = reader.read_int() if flags & 1024 else None
    _forwards = reader.read_int() if flags & 1024 else None
    _replies = reader.tgread_object() if flags & 8388608 else None
    _edit_date = reader.tgread_date() if flags & 32768 else None
    _post_author = reader.tgread_string() if flags & 65536 else None
    _grouped_id = reader.read_long() if flags & 131072 else None
    _reactions = reader.tgread_object() if flags & 1048576 else None
    if flags & 4194304:
        reader.read_int()
        _restriction_reason = [reader.tgread_object() for _ in range(reader.read_int())]
    else:
        _restriction_reason = None
    _ttl_period = reader.read_int() if flags & 33554432 else None
    _quick_reply_shortcut_id = reader.read_int() if flags & 1073741824 else None
    _effect = reader.read_long() if flags2 & 4 else None
    _factcheck = reader.tgread_object() if flags2 & 8 else None
    _report_delivery_until_date = reader.tgread_date() if flags2 & 32 else None
    _paid_message_stars = reader.read_long() if flags2 & 64 else None
    _suggested_post = reader.tgread_object() if flags2 & 128 else None
    _schedule_repeat_period = reader.read_int() if flags2 & 1024 else None
    _summary_from_language = reader.tgread_string() if flags2 & 2048 else None

    message = cls(
        id=_id,
        peer_id=_peer_id,
        date=_date,
        message=_message,
        out=_out,
        mentioned=_mentioned,
        media_unread=_media_unread,
        silent=_silent,
        post=_post,
        from_scheduled=_from_scheduled,
        legacy=_legacy,
        edit_hide=_edit_hide,
        pinned=_pinned,
        noforwards=_noforwards,
        invert_media=_invert_media,
        offline=_offline,
        video_processing_pending=_video_processing_pending,
        paid_suggested_post_stars=_paid_suggested_post_stars,
        paid_suggested_post_ton=_paid_suggested_post_ton,
        from_id=_from_id,
        from_boosts_applied=_from_boosts_applied,
        saved_peer_id=_saved_peer_id,
        fwd_from=_fwd_from,
        via_bot_id=_via_bot_id,
        via_business_bot_id=_via_business_bot_id,
        reply_to=_reply_to,
        media=_media,
        reply_markup=_reply_markup,
        entities=_entities,
        views=_views,
        forwards=_forwards,
        replies=_replies,
        edit_date=_edit_date,
        post_author=_post_author,
        grouped_id=_grouped_id,
        reactions=_reactions,
        restriction_reason=_restriction_reason,
        ttl_period=_ttl_period,
        quick_reply_shortcut_id=_quick_reply_shortcut_id,
        effect=_effect,
        factcheck=_factcheck,
        report_delivery_until_date=_report_delivery_until_date,
        paid_message_stars=_paid_message_stars,
        suggested_post=_suggested_post,
    )
    message.from_rank = _from_rank
    message.guestchat_via_from = _guestchat_via_from
    message.schedule_repeat_period = _schedule_repeat_period
    message.summary_from_language = _summary_from_language
    return message


def install_repeat_support_patch() -> None:
    patched_types.Message.from_reader = classmethod(_patched_message_from_reader)
    types.Message.from_reader = classmethod(_patched_message_from_reader)
    alltlobjects.tlobjects[CURRENT_MESSAGE_CONSTRUCTOR_ID] = patched_types.Message


def read_schedule_repeat_period(message: Any) -> int | None:
    for attr in ("schedule_repeat_period", "schedulePeriod", "schedule_period"):
        raw_value = getattr(message, attr, None)
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None
PROBE_EMOJIS = ("😀", "😄", "😎", "🥳", "✨", "🔥", "🍀", "🌊", "🎯", "🚀")


@dataclass(slots=True)
class LoginResult:
    need_password: bool
    label: str | None = None
    is_premium: bool = False


class TelethonManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.config.session_dir.mkdir(parents=True, exist_ok=True)
        install_repeat_support_patch()
        self._supports_repeat = True

    def session_path(self, session_file: str) -> str:
        return str((self.config.session_dir / session_file).resolve())

    def session_sqlite_path(self, session_file: str) -> Path:
        return Path(self.session_path(session_file)).with_suffix(".session")

    def build_client(self, session_file: str) -> TelegramClient:
        return TelegramClient(
            self.session_path(session_file),
            self.config.api_id,
            self.config.api_hash,
            device_model=self.config.client_device_model,
            system_version=self.config.client_system_version,
            app_version=self.config.client_app_version,
            lang_code=self.config.client_lang_code,
            system_lang_code=self.config.client_system_lang_code,
        )

    def delete_session_files(self, session_file: str) -> None:
        base_path = Path(self.session_path(session_file))
        candidates = [base_path, self.session_sqlite_path(session_file), self.session_sqlite_path(session_file).with_suffix(".session-journal")]
        for item in candidates:
            try:
                if item.exists():
                    item.unlink()
            except OSError:
                continue

    async def inspect_session(self, session_file: str) -> dict[str, Any]:
        client = self.build_client(session_file)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("账号掉线")
            me = await client.get_me()
            label = " ".join(part for part in [getattr(me, "first_name", ""), getattr(me, "last_name", "")] if part).strip()
            phone = getattr(me, "phone", None)
            return {
                "label": label or session_file,
                "phone": f"+{phone}" if phone else "-",
                "is_premium": bool(getattr(me, "premium", False)),
                "username": getattr(me, "username", None),
            }
        finally:
            await client.disconnect()

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

    async def verify_session(self, session_row: dict[str, Any], auto_set_username: bool = False) -> dict[str, Any]:
        client = self.build_client(session_row["session_file"])
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("账号掉线")
            me = await client.get_me()
            username = getattr(me, "username", None)
            username_set = False
            username_error = None
            if auto_set_username and not username:
                username, username_set, username_error = await self._ensure_random_username(
                    client,
                    me,
                    session_row.get("label"),
                )
            return {
                "label": " ".join(part for part in [getattr(me, "first_name", ""), getattr(me, "last_name", "")] if part).strip() or session_row["label"],
                "is_premium": bool(getattr(me, "premium", False)),
                "username": username,
                "username_set": username_set,
                "username_error": username_error,
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
        repeat_period: int | None = None,
    ) -> int:
        client = self.build_client(session_row["session_file"])
        await client.connect()
        try:
            entity = await self._resolve_entity(client, group_row)
            try:
                if repeat_period and repeat_period > 0:
                    if not session_row.get("is_premium"):
                        raise RuntimeError("原生每天重复只支持 Premium 账号")
                    return await self._schedule_native_repeat_message(client, entity, message_text, when, repeat_period)
                message = await client.send_message(entity, message_text, schedule=when)
            except Exception as exc:
                error_message = self.describe_error(exc)
                if error_message != "无发言权限":
                    raise
                joined, note = await self._auto_join_linked_channel_for_speaking(client, entity)
                if not joined:
                    raise RuntimeError(note or error_message)
                if repeat_period and repeat_period > 0:
                    return await self._schedule_native_repeat_message(client, entity, message_text, when, repeat_period)
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
                        "repeat_period": read_schedule_repeat_period(message),
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
            if not await client.is_user_authorized():
                raise RuntimeError("账号掉线")
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
                joined, note = await self._auto_join_linked_channel_for_speaking(client, entity)
                if not joined:
                    message = note or "无发言权限"
                    return {"join_status": "joined", "speak_status": message, "last_error": message}
                permissions = await client.get_permissions(entity, "me")
                if getattr(permissions, "is_banned", False):
                    return {"join_status": "joined", "speak_status": "禁言", "last_error": "禁言"}
                if getattr(permissions, "has_left", False):
                    return {"join_status": "left", "speak_status": "未加入群", "last_error": "账号已离开群"}
            probe_ok, probe_error = await self._probe_send_message(client, entity)
            if probe_ok:
                return {"join_status": "joined", "speak_status": "正常可发", "last_error": ""}
            if probe_error == "无发言权限":
                joined, note = await self._auto_join_linked_channel_for_speaking(client, entity)
                if joined:
                    probe_ok, probe_error = await self._probe_send_message(client, entity)
                    if probe_ok:
                        return {"join_status": "joined", "speak_status": "正常可发", "last_error": ""}
                elif note:
                    probe_error = note
            if probe_error == "未加入群":
                return {"join_status": "not_joined", "speak_status": probe_error, "last_error": probe_error}
            return {"join_status": "joined", "speak_status": probe_error or "无发言权限", "last_error": probe_error or "无发言权限"}
        finally:
            await client.disconnect()

    async def leave_group(self, session_row: dict[str, Any], group_row: dict[str, Any]) -> None:
        client = self.build_client(session_row["session_file"])
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("账号掉线")
            entity = await self._resolve_entity(client, group_row)
            try:
                if getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False):
                    await client(functions.channels.LeaveChannelRequest(entity))
                else:
                    await client.delete_dialog(entity)
            except Exception:
                await client.delete_dialog(entity)
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

    async def _probe_send_message(self, client: TelegramClient, entity: Any) -> tuple[bool, str | None]:
        try:
            probe_message = await client.send_message(entity, random.choice(PROBE_EMOJIS))
        except Exception as exc:
            message = self.describe_error(exc)
            if message == "账号掉线":
                raise
            return False, message
        try:
            await client.delete_messages(entity, [probe_message.id])
        except Exception:
            pass
        return True, None

    async def _auto_join_linked_channel_for_speaking(self, client: TelegramClient, entity: Any) -> tuple[bool, str | None]:
        if not (getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False)):
            return False, None
        try:
            full = await client(GetFullChannelRequest(entity))
        except Exception:
            return False, None
        linked_chat_id = int(getattr(getattr(full, "full_chat", None), "linked_chat_id", 0) or 0)
        if linked_chat_id <= 0:
            return False, None
        linked_entity = None
        for chat in getattr(full, "chats", []) or []:
            if int(getattr(chat, "id", 0) or 0) == linked_chat_id:
                linked_entity = chat
                break
        if linked_entity is None:
            try:
                linked_entity = await client.get_entity(linked_chat_id)
            except Exception as exc:
                return False, f"关联频道解析失败：{self.describe_error(exc)}"
        title = getattr(linked_entity, "title", None) or getattr(linked_entity, "username", None) or str(linked_chat_id)
        try:
            await client(JoinChannelRequest(linked_entity))
            return True, f"已自动关注关联频道：{title}"
        except errors.UserAlreadyParticipantError:
            return True, f"已关注关联频道：{title}"
        except errors.InviteRequestSentError:
            return False, f"关联频道需审批：{title}"
        except Exception as exc:
            return False, f"关注关联频道失败：{self.describe_error(exc)}"

    async def _ensure_random_username(self, client: TelegramClient, me: Any, label: str | None) -> tuple[str | None, bool, str | None]:
        current_username = getattr(me, "username", None)
        if current_username:
            return current_username, False, None
        base = self._username_base(label, getattr(me, "first_name", None), getattr(me, "last_name", None))
        for _ in range(12):
            candidate = self._random_username_candidate(base)
            try:
                user = await client(functions.account.UpdateUsernameRequest(candidate))
                return getattr(user, "username", None) or candidate, True, None
            except (errors.UsernameOccupiedError, errors.UsernameInvalidError):
                continue
            except Exception as exc:
                return None, False, self.describe_error(exc)
        return None, False, "随机用户名生成失败"

    @staticmethod
    def _username_base(*parts: str | None) -> str:
        merged = "".join(part or "" for part in parts).lower()
        merged = re.sub(r"[^a-z0-9]", "", merged)
        merged = re.sub(r"^\d+", "", merged)
        if not merged:
            merged = "u"
        return merged[:10]

    @staticmethod
    def _random_username_candidate(base: str) -> str:
        letters = string.ascii_lowercase + string.digits
        suffix = "".join(random.choice(letters) for _ in range(8))
        candidate = f"{base}{suffix}"
        if len(candidate) < 5:
            candidate = candidate + "".join(random.choice(letters) for _ in range(5 - len(candidate)))
        return candidate[:32]

    async def _schedule_native_repeat_message(
        self,
        client: TelegramClient,
        entity: Any,
        message_text: str,
        when: datetime,
        repeat_period: int,
    ) -> int:
        before_ids = await self._scheduled_message_ids(client, entity)
        request = SendMessageWithRepeatRequest(
            peer=entity,
            message=message_text,
            schedule_date=when,
            schedule_repeat_period=repeat_period,
        )
        try:
            await client(request)
        except Exception:
            matched_id = await self._find_new_scheduled_message_id(client, entity, before_ids, message_text, when, repeat_period)
            if matched_id is not None:
                return matched_id
            raise
        matched_id = await self._find_new_scheduled_message_id(client, entity, before_ids, message_text, when, repeat_period)
        if matched_id is None:
            raise RuntimeError("Telegram 没有返回新建的原生重复定时消息")
        return matched_id

    async def _scheduled_message_ids(self, client: TelegramClient, entity: Any) -> set[int]:
        result = await client(GetScheduledHistoryRequest(peer=entity, hash=0))
        return {int(getattr(message, "id", 0)) for message in getattr(result, "messages", []) if int(getattr(message, "id", 0) or 0) > 0}

    async def _find_new_scheduled_message_id(
        self,
        client: TelegramClient,
        entity: Any,
        before_ids: set[int],
        message_text: str,
        when: datetime,
        repeat_period: int | None = None,
    ) -> int | None:
        target_text = (message_text or "").strip()
        target_iso = self._as_utc(when).isoformat()
        for _ in range(8):
            await asyncio.sleep(0.35)
            result = await client(GetScheduledHistoryRequest(peer=entity, hash=0))
            candidates: list[int] = []
            for message in getattr(result, "messages", []):
                message_id = int(getattr(message, "id", 0) or 0)
                if message_id <= 0 or message_id in before_ids:
                    continue
                date_value = getattr(message, "date", None)
                message_iso = self._as_utc(date_value).isoformat() if isinstance(date_value, datetime) else None
                if target_text and (getattr(message, "message", "") or "").strip() != target_text:
                    continue
                if message_iso and message_iso != target_iso:
                    continue
                actual_repeat = read_schedule_repeat_period(message)
                if repeat_period and actual_repeat not in {None, repeat_period}:
                    continue
                candidates.append(message_id)
            if candidates:
                candidates.sort(reverse=True)
                return candidates[0]
        return None

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def next_daily_run(self, when: datetime, reference: datetime | None = None) -> datetime:
        candidate = self._as_utc(when)
        reference_value = self._as_utc(reference or datetime.now(timezone.utc))
        if candidate >= reference_value:
            return candidate
        delta = reference_value - candidate
        candidate += timedelta(days=delta.days)
        if candidate < reference_value:
            candidate += timedelta(days=1)
        return candidate

    @staticmethod
    def daily_repeat_period() -> int:
        return TELEGRAM_DAILY_REPEAT_PERIOD

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
