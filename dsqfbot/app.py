from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .config import AppConfig, load_config
from .db import Database
from .telegram_mtproto import TelethonManager
from .utils import chunked, format_dt, now_iso, parse_links, parse_user_datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger("dsqfbot")


class DsqfBotApp:
    def __init__(self, config: AppConfig, db: Database, telethon: TelethonManager) -> None:
        self.config = config
        self.db = db
        self.telethon = telethon
        self._tasks: list[asyncio.Task] = []

    async def on_startup(self, application: Application) -> None:
        self._tasks.append(asyncio.create_task(self.join_worker(), name="join-worker"))
        self._tasks.append(asyncio.create_task(self.repeat_worker(), name="repeat-worker"))
        LOGGER.info("workers started")

    async def on_shutdown(self, application: Application) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        LOGGER.info("workers stopped")

    async def ensure_admin(self, update: Update) -> bool:
        user = update.effective_user
        if not user or not self.config.is_admin(user.id):
            if update.effective_message:
                await update.effective_message.reply_text("当前账号没有权限使用这个机器人。")
            return False
        return True

    async def render(self, update: Update, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(text, reply_markup=keyboard)
        elif update.effective_message:
            await update.effective_message.reply_text(text, reply_markup=keyboard)

    def state_cancel_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("取消当前流程", callback_data="state:cancel")]])

    def home_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("账号管理", callback_data="accounts"),
                    InlineKeyboardButton("批量加群", callback_data="join:setup"),
                ],
                [
                    InlineKeyboardButton("定时任务", callback_data="tasks"),
                    InlineKeyboardButton("加群队列", callback_data="join:list"),
                ],
                [
                    InlineKeyboardButton("刷新首页", callback_data="home"),
                    InlineKeyboardButton("取消当前流程", callback_data="state:cancel"),
                ],
            ]
        )

    async def send_home(self, update: Update) -> None:
        sessions = self.db.list_sessions()
        groups_count = sum(len(self.db.list_groups(item["id"])) for item in sessions)
        text = (
            "dsqfbot 面板\n\n"
            f"账号数：{len(sessions)}\n"
            f"群数量：{groups_count}\n"
            f"待处理加群：{len([job for job in self.db.list_join_jobs(50) if job['status'] in ('pending', 'retry', 'running')])}\n"
            f"定时任务：{len(self.db.list_tasks(100))}"
        )
        await self.render(update, text, self.home_keyboard())

    async def finalize_session_login(self, update: Update, user_id: int, payload: dict[str, Any], result: Any) -> None:
        session_id = self.db.create_session(
            label=payload["label"],
            phone=payload["phone"],
            session_file=payload["session_file"],
            is_premium=result.is_premium,
            status="pending",
        )
        self.db.clear_user_state(user_id)
        session_row = self.db.get_session(session_id)
        if not session_row:
            await self.render(update, "账号已登录，但本地保存失败。")
            return
        try:
            info = await self.telethon.verify_session(session_row)
            self.db.update_session(
                session_id,
                status="online",
                is_premium=int(info["is_premium"]),
                label=payload["label"],
                last_error="",
            )
            await self.render(
                update,
                f"账号添加成功：{payload['label']}，Premium：{'是' if info['is_premium'] else '否'}",
                self.account_detail_keyboard(session_id),
            )
        except Exception as exc:
            message = self.telethon.describe_error(exc)
            self.db.update_session(
                session_id,
                status="offline",
                is_premium=int(result.is_premium),
                last_error=f"登录成功，但会话校验失败：{message}",
            )
            await self.render(
                update,
                "验证码已通过，但当前会话没有通过 Telegram 二次校验，账号先记为掉线。\n"
                "这通常是刚登录就被撤销，或当前号码/环境被风控了。",
                self.account_detail_keyboard(session_id),
            )

    def ensure_unique_label(self, label: str, used_labels: set[str] | None = None) -> str:
        existing = used_labels if used_labels is not None else {item["label"] for item in self.db.list_sessions()}
        base = (label or "session").strip() or "session"
        candidate = base
        index = 2
        while candidate in existing:
            candidate = f"{base}-{index}"
            index += 1
        existing.add(candidate)
        return candidate

    def ensure_unique_session_file(self, session_name: str, used_files: set[str] | None = None) -> str:
        existing = used_files if used_files is not None else {item["session_file"] for item in self.db.list_sessions()}
        base = (session_name or "session").strip() or "session"
        candidate = base
        index = 2
        while candidate in existing:
            candidate = f"{base}-{index}"
            index += 1
        existing.add(candidate)
        return candidate

    async def import_session_archive(self, archive_path: Path) -> tuple[list[dict[str, Any]], list[str]]:
        imported: list[dict[str, Any]] = []
        failed: list[str] = []
        used_files = {item["session_file"] for item in self.db.list_sessions()}
        used_labels = {item["label"] for item in self.db.list_sessions()}

        with tempfile.TemporaryDirectory(prefix="dsqfbot-import-") as temp_dir:
            extract_dir = Path(temp_dir) / "unzipped"
            extract_dir.mkdir(parents=True, exist_ok=True)
            with ZipFile(archive_path) as archive:
                archive.extractall(extract_dir)

            session_paths = sorted(path for path in extract_dir.rglob("*.session") if path.is_file())
            if not session_paths:
                raise RuntimeError("压缩包里没有找到 .session 文件")

            for source_path in session_paths:
                session_file = self.ensure_unique_session_file(source_path.stem, used_files)
                target_path = self.telethon.session_sqlite_path(session_file)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)
                try:
                    info = await self.telethon.inspect_session(session_file)
                    label = self.ensure_unique_label(info["label"], used_labels)
                    session_id = self.db.create_session(
                        label=label,
                        phone=info["phone"],
                        session_file=session_file,
                        is_premium=info["is_premium"],
                        status="online",
                    )
                    imported.append(
                        {
                            "id": session_id,
                            "label": label,
                            "phone": info["phone"],
                            "is_premium": info["is_premium"],
                        }
                    )
                except Exception as exc:
                    self.telethon.delete_session_files(session_file)
                    failed.append(f"{source_path.name}: {self.telethon.describe_error(exc)}")
        return imported, failed

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.ensure_admin(update):
            return
        if update.effective_user:
            self.db.clear_user_state(update.effective_user.id)
        await self.send_home(update)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.ensure_admin(update):
            return
        user_id = update.effective_user.id
        state, payload = self.db.get_user_state(user_id)
        text = (update.effective_message.text or "").strip()
        if not state:
            await self.send_home(update)
            return
        try:
            if state == "wait_session_code":
                result = await self.telethon.finish_login(
                    session_file=payload["session_file"],
                    phone=payload["phone"],
                    code=text,
                    phone_code_hash=payload["phone_code_hash"],
                )
                if result.need_password:
                    payload["code"] = text
                    self.db.set_user_state(user_id, "wait_session_password", payload)
                    await self.render(update, "这个号开了二步验证。把二步密码发给我。", self.state_cancel_keyboard())
                    return
                await self.finalize_session_login(update, user_id, payload, result)
                return
            if state == "wait_session_password":
                result = await self.telethon.finish_login(
                    session_file=payload["session_file"],
                    phone=payload["phone"],
                    code=payload.get("code", "00000"),
                    phone_code_hash=payload["phone_code_hash"],
                    password=text,
                )
                await self.finalize_session_login(update, user_id, payload, result)
                return
            if state == "wait_session_label":
                payload["label"] = text
                self.db.set_user_state(user_id, "wait_session_phone", payload)
                await self.render(update, "发这个账号的手机号，格式例子：+8613812345678", self.state_cancel_keyboard())
                return
            if state == "wait_session_phone":
                payload["phone"] = text
                session_file, phone_code_hash = await self.telethon.begin_login(payload["label"], text)
                payload["session_file"] = session_file
                payload["phone_code_hash"] = phone_code_hash
                self.db.set_user_state(user_id, "wait_session_code", payload)
                await self.render(update, "验证码已经发到 Telegram。把验证码直接发给我。", self.state_cancel_keyboard())
                return
            if state == "wait_session_zip":
                await self.render(update, "这里等的是 zip 压缩包文件，不是文字。直接把 session 的 zip 发给我。", self.state_cancel_keyboard())
                return
            if state == "wait_join_links":
                links = parse_links(text)
                if not links:
                    await self.render(update, "没识别到可用的群链接，再发一次。")
                    return
                payload["links"] = links
                self.db.set_user_state(user_id, "wait_join_interval", payload)
                await self.render(update, f"识别到 {len(links)} 个群链接，选一下加群间隔。", self.join_interval_keyboard())
                return
            if state == "wait_schedule_message":
                payload["message_text"] = text
                session_row = self.db.get_session(payload["session_id"])
                if not session_row:
                    self.db.clear_user_state(user_id)
                    await self.render(update, "账号不存在。")
                    return
                self.db.set_user_state(user_id, "wait_schedule_repeat", payload)
                await self.render(update, "选择重复方式。", self.repeat_keyboard(bool(session_row["is_premium"])))
                return
            if state == "wait_schedule_time":
                when = parse_user_datetime(text, self.config.default_timezone)
                if when <= datetime.now(tz=ZoneInfo(self.config.default_timezone)):
                    await self.render(update, "时间必须大于当前时间，格式：2026-09-06 10:30")
                    return
                session_row = self.db.get_session(payload["session_id"])
                group_row = self.db.get_group(payload["group_id"])
                if not session_row or not group_row:
                    self.db.clear_user_state(user_id)
                    await self.render(update, "账号或群不存在。")
                    return
                try:
                    message_id = await self.telethon.schedule_message(session_row, group_row, payload["message_text"], when)
                    next_run_at = None
                    last_scheduled_for = when.isoformat()
                    if payload["repeat_mode"] == "daily":
                        next_run_at = self.telethon.next_daily_run(when).isoformat()
                    task_id = self.db.create_task(
                        session_id=session_row["id"],
                        group_id=group_row["id"],
                        message_text=payload["message_text"],
                        schedule_at=when.isoformat(),
                        repeat_mode=payload["repeat_mode"],
                        next_run_at=next_run_at,
                        last_scheduled_for=last_scheduled_for,
                        last_telegram_message_id=message_id,
                        status="scheduled",
                    )
                    self.db.clear_user_state(user_id)
                    await self.render(update, f"定时消息创建成功，任务 ID：{task_id}", self.task_detail_keyboard(task_id))
                except Exception as exc:
                    message = self.telethon.describe_error(exc)
                    self.db.update_group(group_row["id"], speak_status=message, last_error=message)
                    await self.render(update, f"创建失败：{message}")
                return
        except Exception as exc:
            LOGGER.exception("text handler failed")
            await self.render(update, f"处理失败：{exc}")
            return
        await self.send_home(update)

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.ensure_admin(update):
            return
        user_id = update.effective_user.id
        state, payload = self.db.get_user_state(user_id)
        document = update.effective_message.document if update.effective_message else None
        if not document:
            await self.render(update, "没有收到文件。")
            return
        if state != "wait_session_zip":
            await self.render(update, "当前没有等待导入的 session 压缩包。先去账号管理里点“上传Session压缩包”。", self.accounts_keyboard())
            return

        file_name = document.file_name or "session.zip"
        if not file_name.lower().endswith(".zip"):
            await self.render(update, "只支持上传 .zip 压缩包。", self.state_cancel_keyboard())
            return

        try:
            with tempfile.TemporaryDirectory(prefix="dsqfbot-upload-") as temp_dir:
                archive_path = Path(temp_dir) / file_name
                telegram_file = await context.bot.get_file(document.file_id)
                await telegram_file.download_to_drive(custom_path=str(archive_path))
                imported, failed = await self.import_session_archive(archive_path)
        except BadZipFile:
            await self.render(update, "这个 zip 压缩包打不开，换一个重新发。", self.state_cancel_keyboard())
            return
        except Exception as exc:
            LOGGER.exception("document handler failed")
            await self.render(update, f"导入失败：{exc}", self.state_cancel_keyboard())
            return

        self.db.clear_user_state(user_id)
        lines = [f"导入完成：成功 {len(imported)} 个，失败 {len(failed)} 个"]
        for item in imported[:10]:
            lines.append(f"成功：{item['label']} | {item['phone']} | {'Premium' if item['is_premium'] else '普通'}")
        for item in failed[:10]:
            lines.append(f"失败：{item}")
        await self.render(update, "\n".join(lines), self.accounts_keyboard())

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.ensure_admin(update):
            return
        query = update.callback_query
        assert query
        user_id = query.from_user.id
        data = query.data or ""
        state, payload = self.db.get_user_state(user_id)
        try:
            if data == "home":
                await self.send_home(update)
                return
            if data == "state:cancel":
                self.db.clear_user_state(user_id)
                await self.render(update, "当前流程已取消。", self.home_keyboard())
                return
            if data == "accounts":
                await self.render(update, self.accounts_text(), self.accounts_keyboard())
                return
            if data == "account:add":
                self.db.set_user_state(user_id, "wait_session_label", {})
                await self.render(update, "先发这个账号的备注名字。", self.state_cancel_keyboard())
                return
            if data == "account:import_zip":
                self.db.set_user_state(user_id, "wait_session_zip", {})
                await self.render(update, "把 session 文件打成 zip 压缩包后直接发给我。支持一个 zip 里放多个 .session 文件。", self.state_cancel_keyboard())
                return
            if data.startswith("account:view:"):
                session_id = int(data.split(":")[-1])
                await self.render(update, self.account_detail_text(session_id), self.account_detail_keyboard(session_id))
                return
            if data.startswith("account:refresh:"):
                session_id = int(data.split(":")[-1])
                session_row = self.db.get_session(session_id)
                if not session_row:
                    await self.render(update, "账号不存在。")
                    return
                try:
                    info = await self.telethon.verify_session(session_row)
                    self.db.update_session(session_id, status="online", is_premium=int(info["is_premium"]), label=info["label"], last_error="")
                    await self.render(update, "账号状态已刷新。", self.account_detail_keyboard(session_id))
                except Exception as exc:
                    self.db.update_session(session_id, status="offline", last_error=self.telethon.describe_error(exc))
                    await self.render(update, self.account_detail_text(session_id), self.account_detail_keyboard(session_id))
                return
            if data.startswith("account:sync:"):
                session_id = int(data.split(":")[-1])
                session_row = self.db.get_session(session_id)
                if not session_row:
                    await self.render(update, "账号不存在。")
                    return
                try:
                    items = await self.telethon.list_groups(session_row)
                    for item in items:
                        self.db.upsert_group(session_id, item["peer_id"], item["title"], item["username"], item["link"])
                    self.db.update_session(session_id, status="online", last_error="")
                    await self.render(update, f"同步完成，共 {len(items)} 个群/频道。", self.account_detail_keyboard(session_id))
                except Exception as exc:
                    self.db.update_session(session_id, status="offline", last_error=self.telethon.describe_error(exc))
                    await self.render(update, self.account_detail_text(session_id), self.account_detail_keyboard(session_id))
                return
            if data.startswith("account:delete:"):
                session_id = int(data.split(":")[-1])
                session_row = self.db.get_session(session_id)
                if not session_row:
                    await self.render(update, "账号不存在。")
                    return
                self.telethon.delete_session_files(session_row["session_file"])
                self.db.delete_session(session_id)
                await self.render(update, f"账号已删除：{session_row['label']}", self.accounts_keyboard())
                return
            if data.startswith("account:groups:"):
                session_id = int(data.split(":")[-1])
                await self.render(update, self.groups_text(session_id), self.groups_keyboard(session_id))
                return
            if data.startswith("group:view:"):
                group_id = int(data.split(":")[-1])
                await self.render(update, self.group_detail_text(group_id), self.group_detail_keyboard(group_id))
                return
            if data.startswith("group:scheduled:"):
                group_id = int(data.split(":")[-1])
                group_row = self.db.get_group(group_id)
                if not group_row:
                    await self.render(update, "群不存在。")
                    return
                session_row = self.db.get_session(group_row["session_id"])
                if not session_row:
                    await self.render(update, "账号不存在。")
                    return
                try:
                    messages = await self.telethon.list_scheduled_messages(session_row, group_row)
                    await self.render(update, self.scheduled_messages_text(group_row, messages), self.scheduled_messages_keyboard(group_id))
                except Exception as exc:
                    await self.render(update, f"读取失败：{self.telethon.describe_error(exc)}", self.group_detail_keyboard(group_id))
                return
            if data.startswith("group:refresh:"):
                group_id = int(data.split(":")[-1])
                group_row = self.db.get_group(group_id)
                if not group_row:
                    await self.render(update, "群不存在。")
                    return
                session_row = self.db.get_session(group_row["session_id"])
                if not session_row:
                    await self.render(update, "账号不存在。")
                    return
                result = await self.telethon.detect_group_status(session_row, group_row)
                self.db.update_group(group_id, **result)
                await self.render(update, "群状态已刷新。", self.group_detail_keyboard(group_id))
                return
            if data.startswith("group:schedule:"):
                group_id = int(data.split(":")[-1])
                group_row = self.db.get_group(group_id)
                if not group_row:
                    await self.render(update, "群不存在。")
                    return
                self.db.set_user_state(user_id, "wait_schedule_message", {"group_id": group_id, "session_id": group_row["session_id"]})
                await self.render(update, "把要发送的消息内容直接发给我。", self.state_cancel_keyboard())
                return
            if data.startswith("schedule:repeat:"):
                if state != "wait_schedule_repeat":
                    await self.render(update, "当前没有待创建的定时任务。")
                    return
                repeat_mode = data.split(":")[-1]
                payload["repeat_mode"] = repeat_mode
                self.db.set_user_state(user_id, "wait_schedule_time", payload)
                await self.render(update, f"把发送时间发给我，格式：2026-09-06 10:30\n当前重复：{'每天重复' if repeat_mode == 'daily' else '单次'}", self.state_cancel_keyboard())
                return
            if data == "join:setup":
                payload = {"session_ids": [], "distribution": "balanced"}
                self.db.set_user_state(user_id, "join_select_sessions", payload)
                await self.render(update, "选择要参与加群的账号，然后点下一步。", self.join_setup_keyboard(payload))
                return
            if data.startswith("join:toggle:"):
                if state != "join_select_sessions":
                    payload = {"session_ids": [], "distribution": "balanced"}
                session_id = int(data.split(":")[-1])
                selected = set(payload.get("session_ids", []))
                if session_id in selected:
                    selected.remove(session_id)
                else:
                    selected.add(session_id)
                payload["session_ids"] = sorted(selected)
                self.db.set_user_state(user_id, "join_select_sessions", payload)
                await self.render(update, "选择要参与加群的账号，然后点下一步。", self.join_setup_keyboard(payload))
                return
            if data.startswith("join:mode:"):
                if state != "join_select_sessions":
                    payload = {"session_ids": [], "distribution": "balanced"}
                payload["distribution"] = data.split(":")[-1]
                self.db.set_user_state(user_id, "join_select_sessions", payload)
                await self.render(update, "分配模式已切换。", self.join_setup_keyboard(payload))
                return
            if data == "join:next":
                if state != "join_select_sessions" or not payload.get("session_ids"):
                    await self.render(update, "先选至少一个账号。", self.join_setup_keyboard(payload or {"session_ids": [], "distribution": "balanced"}))
                    return
                self.db.set_user_state(user_id, "wait_join_links", payload)
                await self.render(update, "把群链接批量发给我，一行一个也行。", self.state_cancel_keyboard())
                return
            if data.startswith("join:interval:"):
                if state != "wait_join_interval":
                    await self.render(update, "当前没有待执行的加群批次。")
                    return
                interval = int(data.split(":")[-1])
                summary = self.enqueue_join_jobs(payload, interval)
                self.db.clear_user_state(user_id)
                await self.render(update, summary, self.home_keyboard())
                return
            if data == "join:list":
                await self.render(update, self.join_jobs_text(), self.join_jobs_keyboard())
                return
            if data == "tasks":
                await self.render(update, self.tasks_text(), self.tasks_keyboard())
                return
            if data == "tasks:refresh":
                await self.render(update, self.tasks_text(), self.tasks_keyboard())
                return
            if data.startswith("task:view:"):
                task_id = int(data.split(":")[-1])
                await self.render(update, self.task_detail_text(task_id), self.task_detail_keyboard(task_id))
                return
            if data.startswith("task:delete:"):
                task_id = int(data.split(":")[-1])
                await self.delete_task(task_id)
                await self.render(update, "任务已停用。", self.tasks_keyboard())
                return
        except Exception as exc:
            LOGGER.exception("callback failed")
            await self.render(update, f"执行失败：{exc}")
            return
        await self.send_home(update)

    def accounts_text(self) -> str:
        sessions = self.db.list_sessions()
        if not sessions:
            return "还没有账号，先点“添加账号”。"
        lines = ["账号列表"]
        for item in sessions:
            lines.append(f"{item['id']}. {item['label']} | {item['phone']} | {'Premium' if item['is_premium'] else '普通'} | {self.human_session_status(item['status'])}")
        return "\n".join(lines)

    def accounts_keyboard(self) -> InlineKeyboardMarkup:
        rows = []
        for item in self.db.list_sessions():
            rows.append([InlineKeyboardButton(f"{item['label']} ({'Premium' if item['is_premium'] else '普通'})", callback_data=f"account:view:{item['id']}")])
        rows.append([InlineKeyboardButton("添加账号", callback_data="account:add")])
        rows.append([InlineKeyboardButton("上传Session压缩包", callback_data="account:import_zip")])
        rows.append([InlineKeyboardButton("返回首页", callback_data="home")])
        return InlineKeyboardMarkup(rows)

    def account_detail_text(self, session_id: int) -> str:
        row = self.db.get_session(session_id)
        if not row:
            return "账号不存在。"
        return (
            f"账号：{row['label']}\n"
            f"手机号：{row['phone']}\n"
            f"Premium：{'是' if row['is_premium'] else '否'}\n"
            f"状态：{self.human_session_status(row['status'])}\n"
            f"错误：{row['last_error'] or '-'}"
        )

    def account_detail_keyboard(self, session_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("刷新账号", callback_data=f"account:refresh:{session_id}"),
                    InlineKeyboardButton("同步群组", callback_data=f"account:sync:{session_id}"),
                ],
                [InlineKeyboardButton("删除账号", callback_data=f"account:delete:{session_id}")],
                [InlineKeyboardButton("查看群组", callback_data=f"account:groups:{session_id}")],
                [InlineKeyboardButton("返回账号列表", callback_data="accounts")],
            ]
        )

    def groups_text(self, session_id: int) -> str:
        groups = self.db.list_groups(session_id)
        if not groups:
            return "这个账号还没有同步到群，先点“同步群组”。"
        lines = ["群组列表（最近 20 个）"]
        for item in groups[:20]:
            lines.append(f"{item['id']}. {item['title']} | {self.human_join_status(item['join_status'])} | {item['speak_status']}")
        return "\n".join(lines)

    def groups_keyboard(self, session_id: int) -> InlineKeyboardMarkup:
        groups = self.db.list_groups(session_id)[:20]
        rows = [[InlineKeyboardButton(item["title"][:40], callback_data=f"group:view:{item['id']}")] for item in groups]
        rows.append([InlineKeyboardButton("返回账号详情", callback_data=f"account:view:{session_id}")])
        return InlineKeyboardMarkup(rows)

    def group_detail_text(self, group_id: int) -> str:
        group = self.db.get_group(group_id)
        if not group:
            return "群不存在。"
        return (
            f"群名：{group['title']}\n"
            f"用户名：{group['username'] or '-'}\n"
            f"加入状态：{self.human_join_status(group['join_status'])}\n"
            f"发言状态：{group['speak_status']}\n"
            f"错误：{group['last_error'] or '-'}"
        )

    def group_detail_keyboard(self, group_id: int) -> InlineKeyboardMarkup:
        group = self.db.get_group(group_id)
        session_id = group["session_id"] if group else 0
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("刷新状态", callback_data=f"group:refresh:{group_id}"),
                    InlineKeyboardButton("新建定时消息", callback_data=f"group:schedule:{group_id}"),
                ],
                [InlineKeyboardButton("查看已设定时", callback_data=f"group:scheduled:{group_id}")],
                [InlineKeyboardButton("返回群列表", callback_data=f"account:groups:{session_id}")],
            ]
        )

    def repeat_keyboard(self, allow_daily: bool) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton("单次发送", callback_data="schedule:repeat:once")]]
        if allow_daily:
            rows.append([InlineKeyboardButton("每天重复", callback_data="schedule:repeat:daily")])
        rows.append([InlineKeyboardButton("返回首页", callback_data="home")])
        return InlineKeyboardMarkup(rows)

    def join_setup_keyboard(self, payload: dict[str, Any]) -> InlineKeyboardMarkup:
        selected = set(payload.get("session_ids", []))
        distribution = payload.get("distribution", "balanced")
        rows = []
        for row_items in chunked(self.db.list_sessions(), 2):
            row = []
            for item in row_items:
                prefix = "✅" if item["id"] in selected else "▫️"
                row.append(InlineKeyboardButton(f"{prefix}{item['label']}", callback_data=f"join:toggle:{item['id']}"))
            rows.append(row)
        rows.append(
            [
                InlineKeyboardButton(
                    f"{'✅' if distribution == 'balanced' else '▫️'}均分分配",
                    callback_data="join:mode:balanced",
                ),
                InlineKeyboardButton(
                    f"{'✅' if distribution == 'all' else '▫️'}全部都加",
                    callback_data="join:mode:all",
                ),
            ]
        )
        rows.append([InlineKeyboardButton("下一步", callback_data="join:next")])
        rows.append([InlineKeyboardButton("返回首页", callback_data="home")])
        return InlineKeyboardMarkup(rows)

    def join_interval_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("30 秒", callback_data="join:interval:30"),
                    InlineKeyboardButton("60 秒", callback_data="join:interval:60"),
                    InlineKeyboardButton("120 秒", callback_data="join:interval:120"),
                ],
                [
                    InlineKeyboardButton("180 秒", callback_data="join:interval:180"),
                    InlineKeyboardButton("300 秒", callback_data="join:interval:300"),
                ],
                [InlineKeyboardButton("返回首页", callback_data="home")],
            ]
        )

    def enqueue_join_jobs(self, payload: dict[str, Any], interval: int) -> str:
        session_ids = payload["session_ids"]
        links = payload["links"]
        mode = payload.get("distribution", "balanced")
        now = datetime.utcnow()
        created = 0
        if mode == "all":
            for index, link in enumerate(links):
                for offset, session_id in enumerate(session_ids):
                    scheduled_at = (now + timedelta(seconds=interval * (created))).replace(microsecond=0).isoformat()
                    self.db.create_join_job(session_id, link, mode, scheduled_at)
                    created += 1
        else:
            for index, link in enumerate(links):
                session_id = session_ids[index % len(session_ids)]
                scheduled_at = (now + timedelta(seconds=interval * created)).replace(microsecond=0).isoformat()
                self.db.create_join_job(session_id, link, mode, scheduled_at)
                created += 1
        return f"已创建 {created} 条加群任务，模式：{'全部都加' if mode == 'all' else '均分分配'}，间隔：{interval} 秒。"

    def join_jobs_text(self) -> str:
        jobs = self.db.list_join_jobs()
        if not jobs:
            return "当前没有加群任务。"
        lines = ["加群队列"]
        for item in jobs:
            session_row = self.db.get_session(item["session_id"])
            label = session_row["label"] if session_row else str(item["session_id"])
            lines.append(f"{item['id']}. {label} | {self.human_join_job_status(item['status'])} | {item['link']}")
        return "\n".join(lines)

    def join_jobs_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("刷新队列", callback_data="join:list")],
                [InlineKeyboardButton("返回首页", callback_data="home")],
            ]
        )

    def tasks_text(self) -> str:
        tasks = self.db.list_tasks()
        if not tasks:
            return "还没有定时任务。"
        lines = ["定时任务列表"]
        for item in tasks:
            repeat_text = "每天" if item["repeat_mode"] == "daily" else "单次"
            lines.append(f"{item['id']}. {item['group_title']} | {format_dt(item['schedule_at'], self.config.default_timezone)} | {repeat_text} | {self.human_task_status(item['status'])}")
        return "\n".join(lines)

    def tasks_keyboard(self) -> InlineKeyboardMarkup:
        tasks = self.db.list_tasks()
        rows = [[InlineKeyboardButton(f"{item['id']}. {item['group_title'][:30]}", callback_data=f"task:view:{item['id']}")] for item in tasks[:20]]
        rows.append([InlineKeyboardButton("刷新任务", callback_data="tasks:refresh")])
        rows.append([InlineKeyboardButton("返回首页", callback_data="home")])
        return InlineKeyboardMarkup(rows)

    def task_detail_text(self, task_id: int) -> str:
        item = self.db.get_task(task_id)
        if not item:
            return "任务不存在。"
        return (
            f"任务 ID：{item['id']}\n"
            f"账号：{item['session_label']}\n"
            f"群：{item['group_title']}\n"
            f"时间：{format_dt(item['schedule_at'], self.config.default_timezone)}\n"
            f"重复：{'每天' if item['repeat_mode'] == 'daily' else '单次'}\n"
            f"状态：{self.human_task_status(item['status'])}\n"
            f"错误：{item['last_error'] or '-'}"
        )

    def scheduled_messages_text(self, group_row: dict[str, Any], messages: list[dict[str, Any]]) -> str:
        if not messages:
            return f"{group_row['title']}\n\n当前 Telegram 里还没有已设定时消息。"
        lines = [f"{group_row['title']} 的已设定时消息"]
        for item in messages[:10]:
            preview = (item["text"] or "").replace("\n", " ")
            if len(preview) > 24:
                preview = preview[:24] + "..."
            lines.append(f"{item['message_id']}. {format_dt(item['schedule_at'], self.config.default_timezone)} | {preview or '[空消息]'}")
        return "\n".join(lines)

    def scheduled_messages_keyboard(self, group_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("刷新已设定时", callback_data=f"group:scheduled:{group_id}")],
                [InlineKeyboardButton("返回群详情", callback_data=f"group:view:{group_id}")],
            ]
        )

    @staticmethod
    def human_session_status(status: str) -> str:
        return {
            "online": "在线",
            "offline": "掉线",
            "pending": "待登录",
        }.get(status, status)

    @staticmethod
    def human_join_status(status: str) -> str:
        return {
            "joined": "已加入",
            "awaiting_approval": "等待审批",
            "not_joined": "未加入",
            "left": "已离开",
        }.get(status, status)

    @staticmethod
    def human_join_job_status(status: str) -> str:
        return {
            "pending": "排队中",
            "retry": "等待重试",
            "running": "执行中",
            "joined": "已加入",
            "awaiting_approval": "等待审批",
            "failed": "失败",
        }.get(status, status)

    @staticmethod
    def human_task_status(status: str) -> str:
        return {
            "scheduled": "已设定",
            "cancelled": "已停用",
            "failed": "失败",
        }.get(status, status)

    def task_detail_keyboard(self, task_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("停用任务", callback_data=f"task:delete:{task_id}")],
                [InlineKeyboardButton("返回任务列表", callback_data="tasks")],
            ]
        )

    async def delete_task(self, task_id: int) -> None:
        task = self.db.get_task(task_id)
        if not task:
            return
        group_row = self.db.get_group(task["group_id"])
        session_row = self.db.get_session(task["session_id"])
        if group_row and session_row and task.get("last_telegram_message_id"):
            try:
                await self.telethon.delete_scheduled_message(session_row, group_row, int(task["last_telegram_message_id"]))
            except Exception as exc:
                LOGGER.warning("delete scheduled message failed: %s", exc)
        self.db.update_task(task_id, status="cancelled", next_run_at=None)

    async def join_worker(self) -> None:
        while True:
            job = self.db.claim_due_join_job()
            if not job:
                await asyncio.sleep(5)
                continue
            session_row = self.db.get_session(job["session_id"])
            if not session_row:
                self.db.finish_join_job(job["id"], "failed", last_error="账号不存在")
                continue
            try:
                result = await self.telethon.join_link(session_row, job["link"])
                group_id = None
                if int(result.get("peer_id") or 0):
                    group_id = self.db.upsert_group(
                        session_id=session_row["id"],
                        peer_id=int(result["peer_id"]),
                        title=result["title"],
                        username=result.get("username"),
                        link=result.get("link"),
                        join_status=result.get("join_status", "joined"),
                    )
                self.db.finish_join_job(job["id"], result.get("join_status", "joined"), group_id=group_id)
            except Exception as exc:
                message = self.telethon.describe_error(exc)
                if "风控等待" in message:
                    retry_at = (datetime.utcnow() + timedelta(seconds=self.telethon.extract_wait_seconds(exc))).replace(microsecond=0).isoformat()
                    self.db.retry_join_job(job["id"], retry_at, message)
                else:
                    self.db.finish_join_job(job["id"], "failed", last_error=message)
                    self.db.update_session(session_row["id"], status="offline" if message == "账号掉线" else session_row["status"], last_error=message)
            await asyncio.sleep(1)

    async def repeat_worker(self) -> None:
        while True:
            horizon = (datetime.utcnow() + timedelta(minutes=self.config.repeat_lookahead_minutes)).replace(microsecond=0).isoformat()
            for task in self.db.list_due_repeat_tasks(horizon):
                session_row = self.db.get_session(task["session_id"])
                group_row = self.db.get_group(task["group_id"])
                if not session_row or not group_row:
                    self.db.update_task(task["id"], status="failed", last_error="账号或群不存在")
                    continue
                when = datetime.fromisoformat(task["next_run_at"])
                try:
                    message_id = await self.telethon.schedule_message(session_row, group_row, task["message_text"], when)
                    self.db.update_task(
                        task["id"],
                        last_telegram_message_id=message_id,
                        last_scheduled_for=task["next_run_at"],
                        next_run_at=self.telethon.next_daily_run(when).isoformat(),
                        last_error="",
                    )
                except Exception as exc:
                    message = self.telethon.describe_error(exc)
                    self.db.update_task(task["id"], last_error=message)
                    self.db.update_group(group_row["id"], speak_status=message, last_error=message)
                    if message == "账号掉线":
                        self.db.update_session(session_row["id"], status="offline", last_error=message)
            await asyncio.sleep(30)


def build_application(config: AppConfig, db: Database, telethon: TelethonManager) -> Application:
    runtime = DsqfBotApp(config, db, telethon)
    application = (
        ApplicationBuilder()
        .token(config.bot_token)
        .post_init(runtime.on_startup)
        .post_shutdown(runtime.on_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", runtime.on_start))
    application.add_handler(CallbackQueryHandler(runtime.on_callback))
    application.add_handler(MessageHandler(filters.Document.ALL, runtime.on_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, runtime.on_text))
    return application


def main() -> None:
    base_dir = Path(__file__).resolve().parents[1]
    config = load_config(base_dir)
    if not config.bot_token:
        raise RuntimeError("请先在 .env 里填写 BOT_TOKEN")
    if not config.api_id or not config.api_hash:
        raise RuntimeError("请先在 .env 里填写 API_ID 和 API_HASH")
    db = Database(config.database_path)
    telethon = TelethonManager(config)
    application = build_application(config, db, telethon)
    application.run_polling(close_loop=False)
