from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .config import AppConfig, load_config
from .db import Database
from .telegram_mtproto import TelethonManager
from .utils import chunked, format_dt, now_iso, parse_links, parse_user_datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger("dsqfbot")
TELEGRAM_SCHEDULE_LIMIT = 100
TASKS_PAGE_SIZE = 10


class DsqfBotApp:
    def __init__(self, config: AppConfig, db: Database, telethon: TelethonManager) -> None:
        self.config = config
        self.db = db
        self.telethon = telethon
        self.application: Application | None = None
        self._tasks: list[asyncio.Task] = []

    async def on_startup(self, application: Application) -> None:
        self.application = application
        self._tasks.append(asyncio.create_task(self.join_worker(), name="join-worker"))
        self._tasks.append(asyncio.create_task(self.repeat_worker(), name="repeat-worker"))
        LOGGER.info("workers started")

    async def on_shutdown(self, application: Application) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self.application = None
        LOGGER.info("workers stopped")

    async def ensure_admin(self, update: Update) -> bool:
        user = update.effective_user
        if not user or not self.config.is_admin(user.id):
            if update.effective_message:
                await update.effective_message.reply_text("当前账号没有权限使用这个机器人。")
            return False
        return True

    async def render(self, update: Update, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
        await self.render_message(update, text, keyboard)

    async def render_message(self, update: Update, text: str, keyboard: InlineKeyboardMarkup | None = None) -> Any:
        if update.callback_query:
            await update.callback_query.answer()
            message = update.callback_query.message
            if message:
                try:
                    await message.edit_text(text, reply_markup=keyboard)
                    return message
                except BadRequest as exc:
                    if "Message is not modified" in str(exc):
                        return message
                    LOGGER.warning("edit callback message failed: %s", exc)
                    return await message.reply_text(text, reply_markup=keyboard)
        elif update.effective_message:
            return await update.effective_message.reply_text(text, reply_markup=keyboard)
        return None

    async def edit_message(self, message: Any, text: str, keyboard: InlineKeyboardMarkup | None = None) -> bool:
        if not message:
            return False
        try:
            await message.edit_text(text, reply_markup=keyboard)
            return True
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                return True
            LOGGER.warning("edit message failed: %s", exc)
        except Exception as exc:
            LOGGER.warning("edit message failed: %s", exc)
        return False

    async def edit_message_ref(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int | None,
        message_id: int | None,
        text: str,
        keyboard: InlineKeyboardMarkup | None = None,
    ) -> bool:
        if not chat_id or not message_id:
            return False
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
            )
            return True
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                return True
            LOGGER.warning("edit message by ref failed: %s", exc)
        except Exception as exc:
            LOGGER.warning("edit message by ref failed: %s", exc)
        return False

    async def edit_message_by_bot(
        self,
        chat_id: int | None,
        message_id: int | None,
        text: str,
        keyboard: InlineKeyboardMarkup | None = None,
    ) -> bool:
        if not self.application or not chat_id or not message_id:
            return False
        try:
            await self.application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
            )
            return True
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                return True
            LOGGER.warning("edit message by bot failed: %s", exc)
        except Exception as exc:
            LOGGER.warning("edit message by bot failed: %s", exc)
        return False

    async def refresh_join_batch_message(self, batch_id: int, final: bool = False) -> None:
        batch = self.db.get_join_batch(batch_id)
        if not batch:
            return
        text = self.join_batch_progress_text(batch_id)
        if final and "批量加群已完成" not in text:
            text = text.replace("批量加群进行中", "批量加群已完成", 1)
        await self.edit_message_by_bot(
            batch.get("notify_chat_id"),
            batch.get("notify_message_id"),
            text,
            self.join_jobs_keyboard(),
        )

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
            f"定时任务：{self.db.count_tasks()}"
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
            if state == "wait_task_lookup":
                if not text.isdigit():
                    await self.render(update, "把要查看的任务编号发给我，例如：102", self.state_cancel_keyboard())
                    return
                task_id = int(text)
                if not self.db.get_task(task_id):
                    await self.render(update, "没找到这个任务编号，再发一次。", self.state_cancel_keyboard())
                    return
                self.db.clear_user_state(user_id)
                return_page = int(payload.get("page", 0) or 0)
                await self.render(update, self.task_detail_text(task_id), self.task_detail_keyboard(task_id, return_page))
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
            if state == "wait_schedule_interval":
                interval_text = text.strip()
                if not interval_text.isdigit():
                    await self.render(
                        update,
                        "把自定义间隔分钟数发给我，例如：7 或 15",
                        self.state_cancel_keyboard(),
                    )
                    return
                interval_minutes = int(interval_text)
                if interval_minutes <= 0 or interval_minutes > 1440:
                    await self.render(
                        update,
                        "自定义间隔需要在 1 到 1440 分钟之间。",
                        self.state_cancel_keyboard(),
                    )
                    return
                payload["repeat_mode"] = f"interval:{interval_minutes}"
                self.db.set_user_state(user_id, "wait_schedule_time", payload)
                prompt = (
                    "把第一条发送时间发给我，格式：2026-09-06 10:30\n"
                    f"当前模式：每 {interval_minutes} 分钟自动往后排，直到 Telegram 定时上限"
                )
                prompt_message = await self.render_message(update, prompt, self.state_cancel_keyboard())
                if prompt_message:
                    payload["prompt_chat_id"] = getattr(prompt_message, "chat_id", None)
                    payload["prompt_message_id"] = getattr(prompt_message, "message_id", None)
                    self.db.set_user_state(user_id, "wait_schedule_time", payload)
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
                progress_state = {"created": 0, "total": 0, "last_when": when}
                progress_message = None
                progress_chat_id = payload.get("prompt_chat_id")
                progress_message_id = payload.get("prompt_message_id")
                try:
                    interval_minutes = self.interval_repeat_minutes(payload["repeat_mode"])
                    if interval_minutes is not None:
                        initial_text = (
                            "批量定时创建中：0/?\n"
                            f"间隔：每 {interval_minutes} 分钟\n"
                            "正在读取当前已设定时，并准备批量创建..."
                        )

                        async def update_progress_text(
                            current_text: str,
                            keyboard: InlineKeyboardMarkup | None = None,
                        ) -> bool:
                            if await self.edit_message_ref(
                                context,
                                progress_chat_id,
                                progress_message_id,
                                current_text,
                                keyboard,
                            ):
                                return True
                            return await self.edit_message(progress_message, current_text, keyboard)

                        if not await update_progress_text(initial_text, self.state_cancel_keyboard()) and update.effective_message:
                            progress_message = await update.effective_message.reply_text(
                                initial_text,
                                reply_markup=self.state_cancel_keyboard(),
                            )
                            progress_chat_id = getattr(progress_message, "chat_id", None)
                            progress_message_id = getattr(progress_message, "message_id", None)

                        async def report_progress_v2(created: int, total: int, current_when: datetime) -> None:
                            progress_state["created"] = created
                            progress_state["total"] = total
                            progress_state["last_when"] = current_when
                            if created not in {1, total} and created % 5 != 0:
                                return
                            await update_progress_text(
                                (
                                    f"批量定时创建中：{created}/{total}\n"
                                    f"间隔：每 {interval_minutes} 分钟\n"
                                    f"当前排到：{format_dt(current_when.isoformat(), self.config.default_timezone)}"
                                ),
                                self.state_cancel_keyboard(),
                            )

                        try:
                            task_ids, total = await self.create_interval_schedule_batch(
                                session_row=session_row,
                                group_row=group_row,
                                message_text=payload["message_text"],
                                first_when=when,
                                interval_minutes=interval_minutes,
                                progress_callback=report_progress_v2,
                            )
                        except Exception as exc:
                            message = self.telethon.describe_error(exc)
                            self.db.update_group(group_row["id"], speak_status=message, last_error=message)
                            failed_text = f"批量创建失败：已创建 {progress_state['created']} / {progress_state['total'] or '?'} 条。\n错误：{message}"
                            if not await update_progress_text(failed_text):
                                await self.render(update, failed_text)
                            return

                        self.db.clear_user_state(user_id)
                        last_when = when + timedelta(minutes=interval_minutes * (len(task_ids) - 1))
                        final_text = (
                            f"批量定时创建成功，共 {len(task_ids)} / {total} 条。\n"
                            f"间隔：每 {interval_minutes} 分钟\n"
                            f"开始：{format_dt(when.isoformat(), self.config.default_timezone)}\n"
                            f"最后一条：{format_dt(last_when.isoformat(), self.config.default_timezone)}"
                        )
                        if not await update_progress_text(final_text, self.tasks_keyboard()):
                            await self.render(update, final_text, self.tasks_keyboard())
                        return
                        if update.effective_message:
                            progress_message = await update.effective_message.reply_text("正在读取当前已设定时，并准备批量创建…")

                        async def report_progress(created: int, total: int, current_when: datetime) -> None:
                            progress_state["created"] = created
                            progress_state["total"] = total
                            progress_state["last_when"] = current_when
                            if not progress_message:
                                return
                            if created not in {1, total} and created % 5 != 0:
                                return
                            await self.edit_message(
                                progress_message,
                                (
                                    f"批量定时创建中：{created}/{total}\n"
                                    f"间隔：每 {interval_minutes} 分钟\n"
                                    f"当前排到：{format_dt(current_when.isoformat(), self.config.default_timezone)}"
                                ),
                            )

                        task_ids, total = await self.create_interval_schedule_batch(
                            session_row=session_row,
                            group_row=group_row,
                            message_text=payload["message_text"],
                            first_when=when,
                            interval_minutes=interval_minutes,
                            progress_callback=report_progress,
                        )
                        self.db.clear_user_state(user_id)
                        last_when = when + timedelta(minutes=interval_minutes * (len(task_ids) - 1))
                        final_text = (
                            f"批量定时创建成功，共 {len(task_ids)} / {total} 条。\n"
                            f"间隔：每 {interval_minutes} 分钟\n"
                            f"开始：{format_dt(when.isoformat(), self.config.default_timezone)}\n"
                            f"最后一条：{format_dt(last_when.isoformat(), self.config.default_timezone)}"
                        )
                        if not await self.edit_message(progress_message, final_text, self.tasks_keyboard()):
                            await self.render(update, final_text, self.tasks_keyboard())
                        return

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
                    if interval_minutes is not None:
                        failed_text = f"批量创建失败：已创建 {progress_state['created']} / {progress_state['total'] or '?'} 条。\n错误：{message}"
                        if not await self.edit_message(progress_message, failed_text):
                            await self.render(update, failed_text)
                    else:
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
                await self.render(update, self.group_detail_text(group_id), self.group_detail_keyboard(group_id))
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
                repeat_mode = data.removeprefix("schedule:repeat:")
                if repeat_mode == "interval:custom":
                    self.db.set_user_state(user_id, "wait_schedule_interval", payload)
                    await self.render(
                        update,
                        "把自定义间隔分钟数发给我，例如：7 或 15",
                        self.state_cancel_keyboard(),
                    )
                    return
                payload["repeat_mode"] = repeat_mode
                self.db.set_user_state(user_id, "wait_schedule_time", payload)
                interval_minutes = self.interval_repeat_minutes(repeat_mode)
                if interval_minutes is not None:
                    prompt = (
                        "把第一条发送时间发给我，格式：2026-09-06 10:30\n"
                        f"当前模式：每 {interval_minutes} 分钟自动往后排，直到 Telegram 定时上限"
                    )
                else:
                    prompt = f"把发送时间发给我，格式：2026-09-06 10:30\n当前重复：{self.repeat_mode_text(repeat_mode)}"
                prompt_message = await self.render_message(update, prompt, self.state_cancel_keyboard())
                if prompt_message:
                    payload["prompt_chat_id"] = getattr(prompt_message, "chat_id", None)
                    payload["prompt_message_id"] = getattr(prompt_message, "message_id", None)
                    self.db.set_user_state(user_id, "wait_schedule_time", payload)
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
                mode = payload.get("distribution", "balanced")
                total_jobs = len(payload.get("links", [])) * (len(payload.get("session_ids", [])) if mode == "all" else 1)
                batch_id = self.db.create_join_batch(mode=mode, interval_seconds=interval, total_jobs=total_jobs)
                created, summary = self.enqueue_join_jobs(payload, interval, batch_id=batch_id)
                self.db.update_join_batch(batch_id, total_jobs=created)
                self.db.clear_user_state(user_id)
                progress_text = self.join_batch_progress_text(batch_id)
                progress_message = await self.render_message(update, f"{summary}\n\n{progress_text}", self.join_jobs_keyboard())
                if progress_message:
                    self.db.update_join_batch(
                        batch_id,
                        notify_chat_id=getattr(progress_message, "chat_id", None),
                        notify_message_id=getattr(progress_message, "message_id", None),
                    )
                return
            if data == "join:list":
                await self.render(update, self.join_jobs_text(), self.join_jobs_keyboard())
                return
            if data == "tasks":
                await self.render(update, self.tasks_text(), self.tasks_keyboard())
                return
            if data.startswith("tasks:page:"):
                page = self.parse_callback_page(data, "tasks:page:")
                await self.render(update, self.tasks_text(page), self.tasks_keyboard(page))
                return
            if data.startswith("tasks:refresh"):
                page = self.parse_callback_page(data, "tasks:refresh:")
                await self.render(update, self.tasks_text(page), self.tasks_keyboard(page))
                return
            if data.startswith("tasks:pick"):
                page = self.parse_callback_page(data, "tasks:pick:")
                self.db.set_user_state(user_id, "wait_task_lookup", {"page": page})
                await self.render(update, "把要查看的任务编号发给我。", self.state_cancel_keyboard())
                return
            if data.startswith("task:view:"):
                parts = data.split(":")
                task_id = int(parts[2])
                return_page = int(parts[3]) if len(parts) > 3 else 0
                await self.render(update, self.task_detail_text(task_id), self.task_detail_keyboard(task_id, return_page))
                return
            if data.startswith("task:delete:"):
                parts = data.split(":")
                task_id = int(parts[2])
                return_page = int(parts[3]) if len(parts) > 3 else 0
                await self.delete_task(task_id)
                await self.render(update, f"任务已停用。\n\n{self.tasks_text(return_page)}", self.tasks_keyboard(return_page))
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
        rows = [
            [InlineKeyboardButton("单次发送", callback_data="schedule:repeat:once")],
            [
                InlineKeyboardButton("每 5 分钟排满", callback_data="schedule:repeat:interval:5"),
                InlineKeyboardButton("每 10 分钟排满", callback_data="schedule:repeat:interval:10"),
            ],
            [
                InlineKeyboardButton("每 30 分钟排满", callback_data="schedule:repeat:interval:30"),
                InlineKeyboardButton("自定义间隔排满", callback_data="schedule:repeat:interval:custom"),
            ],
        ]
        if allow_daily:
            rows.append([InlineKeyboardButton("每天重复", callback_data="schedule:repeat:daily")])
        rows.append([InlineKeyboardButton("返回首页", callback_data="home")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def interval_repeat_minutes(repeat_mode: str) -> int | None:
        if not repeat_mode.startswith("interval:"):
            return None
        try:
            minutes = int(repeat_mode.split(":")[-1])
        except ValueError:
            return None
        return minutes if minutes > 0 else None

    def repeat_mode_text(self, repeat_mode: str) -> str:
        if repeat_mode == "daily":
            return "每天"
        interval_minutes = self.interval_repeat_minutes(repeat_mode)
        if interval_minutes is not None:
            return f"每 {interval_minutes} 分钟排满"
        return "单次"

    async def create_interval_schedule_batch(
        self,
        session_row: dict[str, Any],
        group_row: dict[str, Any],
        message_text: str,
        first_when: datetime,
        interval_minutes: int,
        progress_callback: Callable[[int, int, datetime], Awaitable[None]] | None = None,
    ) -> tuple[list[int], int]:
        existing = await self.telethon.list_scheduled_messages(session_row, group_row)
        remaining = max(TELEGRAM_SCHEDULE_LIMIT - len(existing), 0)
        if remaining <= 0:
            raise RuntimeError("这个群的 Telegram 已设定时已经到上限了")

        created_task_ids: list[int] = []
        for offset in range(remaining):
            when = first_when + timedelta(minutes=interval_minutes * offset)
            message_id = await self.telethon.schedule_message(session_row, group_row, message_text, when)
            task_id = self.db.create_task(
                session_id=session_row["id"],
                group_id=group_row["id"],
                message_text=message_text,
                schedule_at=when.isoformat(),
                repeat_mode="once",
                next_run_at=None,
                last_scheduled_for=when.isoformat(),
                last_telegram_message_id=message_id,
                status="scheduled",
            )
            created_task_ids.append(task_id)
            if progress_callback:
                await progress_callback(len(created_task_ids), remaining, when)
        return created_task_ids, remaining

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

    def enqueue_join_jobs(self, payload: dict[str, Any], interval: int, batch_id: int | None = None) -> tuple[int, str]:
        session_ids = payload["session_ids"]
        links = payload["links"]
        mode = payload.get("distribution", "balanced")
        now = datetime.utcnow()
        created = 0
        if mode == "all":
            for index, link in enumerate(links):
                for offset, session_id in enumerate(session_ids):
                    scheduled_at = (now + timedelta(seconds=interval * (created))).replace(microsecond=0).isoformat()
                    self.db.create_join_job(session_id, link, mode, scheduled_at, batch_id=batch_id)
                    created += 1
        else:
            for index, link in enumerate(links):
                session_id = session_ids[index % len(session_ids)]
                scheduled_at = (now + timedelta(seconds=interval * created)).replace(microsecond=0).isoformat()
                self.db.create_join_job(session_id, link, mode, scheduled_at, batch_id=batch_id)
                created += 1
        return created, f"已创建 {created} 条加群任务，模式：{'全部都加' if mode == 'all' else '均分分配'}，间隔：{interval} 秒。"

    def join_batch_progress_text(self, batch_id: int) -> str:
        batch = self.db.get_join_batch(batch_id)
        stats = self.db.join_batch_stats(batch_id)
        total = int(batch["total_jobs"]) if batch else stats["total"]
        done = stats["joined"] + stats["awaiting_approval"] + stats["failed"]
        mode_text = "全部都加" if batch and batch["mode"] == "all" else "均分分配"
        interval_seconds = int(batch["interval_seconds"]) if batch else 0
        lines = [
            f"批量加群进行中：{done}/{total}",
            f"模式：{mode_text}",
            f"间隔：{interval_seconds} 秒",
            f"成功：{stats['joined']}",
            f"待审批：{stats['awaiting_approval']}",
            f"失败：{stats['failed']}",
            f"排队中：{stats['pending']}",
            f"重试中：{stats['retry']}",
            f"执行中：{stats['running']}",
        ]
        recent_jobs = self.db.list_join_jobs(20)
        recent_failures = [item for item in recent_jobs if item.get("batch_id") == batch_id and item["status"] == "failed" and item.get("last_error")]
        if recent_failures:
            lines.append("")
            lines.append(f"最近失败：{recent_failures[0]['link']}")
            lines.append(f"原因：{recent_failures[0]['last_error']}")
        if done >= total and total > 0:
            lines[0] = f"批量加群已完成：{done}/{total}"
        return "\n".join(lines)

    def join_jobs_text(self) -> str:
        jobs = self.db.list_join_jobs()
        if not jobs:
            return "当前没有加群任务。"
        stats = self.db.join_job_stats()
        lines = [
            "加群队列",
            f"成功 {stats['joined']} | 待审批 {stats['awaiting_approval']} | 失败 {stats['failed']} | 排队中 {stats['pending']} | 重试中 {stats['retry']} | 执行中 {stats['running']}",
        ]
        for item in jobs:
            session_row = self.db.get_session(item["session_id"])
            label = session_row["label"] if session_row else str(item["session_id"])
            extra = f" | 原因：{item['last_error']}" if item.get("last_error") else ""
            lines.append(f"{item['id']}. {label} | {self.human_join_job_status(item['status'])} | {item['link']}{extra}")
        return "\n".join(lines)

    def join_jobs_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("刷新队列", callback_data="join:list")],
                [InlineKeyboardButton("返回首页", callback_data="home")],
            ]
        )

    def prune_completed_once_tasks(self) -> int:
        cutoff = datetime.now(tz=ZoneInfo(self.config.default_timezone)) - timedelta(minutes=1)
        return self.db.delete_completed_once_tasks(cutoff.isoformat())

    @staticmethod
    def parse_callback_page(data: str, prefix: str) -> int:
        if not data.startswith(prefix):
            return 0
        raw = data.removeprefix(prefix).strip()
        if not raw:
            return 0
        try:
            return max(0, int(raw))
        except ValueError:
            return 0

    def task_page_items(self, page: int = 0) -> tuple[list[dict[str, Any]], int, int]:
        self.prune_completed_once_tasks()
        total = self.db.count_tasks()
        if total <= 0:
            return [], 0, 0
        max_page = max(0, (total - 1) // TASKS_PAGE_SIZE)
        current_page = max(0, min(page, max_page))
        tasks = self.db.list_tasks(limit=TASKS_PAGE_SIZE, offset=current_page * TASKS_PAGE_SIZE)
        return tasks, total, current_page

    def tasks_text(self, page: int = 0) -> str:
        tasks, total, current_page = self.task_page_items(page)
        if not tasks:
            return "还没有定时任务。"
        total_pages = max(1, (total + TASKS_PAGE_SIZE - 1) // TASKS_PAGE_SIZE)
        lines = [f"定时任务列表（第 {current_page + 1}/{total_pages} 页，共 {total} 条）", "点下方“查看任务”后，把任务编号发给我。"]
        for item in tasks:
            repeat_text = self.repeat_mode_text(item["repeat_mode"])
            lines.append(f"{item['id']}. {item['group_title']} | {format_dt(item['schedule_at'], self.config.default_timezone)} | {repeat_text} | {self.human_task_status(item['status'])}")
        return "\n".join(lines)

    def tasks_keyboard(self, page: int = 0) -> InlineKeyboardMarkup:
        tasks, total, current_page = self.task_page_items(page)
        rows: list[list[InlineKeyboardButton]] = []
        if tasks:
            rows.append([InlineKeyboardButton("查看任务", callback_data=f"tasks:pick:{current_page}")])
        nav_row: list[InlineKeyboardButton] = []
        if current_page > 0:
            nav_row.append(InlineKeyboardButton("上一页", callback_data=f"tasks:page:{current_page - 1}"))
        if (current_page + 1) * TASKS_PAGE_SIZE < total:
            nav_row.append(InlineKeyboardButton("下一页", callback_data=f"tasks:page:{current_page + 1}"))
        if nav_row:
            rows.append(nav_row)
        rows.append(
            [
                InlineKeyboardButton("刷新任务", callback_data=f"tasks:refresh:{current_page}"),
                InlineKeyboardButton("返回首页", callback_data="home"),
            ]
        )
        return InlineKeyboardMarkup(rows)

    def task_detail_text(self, task_id: int) -> str:
        self.prune_completed_once_tasks()
        item = self.db.get_task(task_id)
        if not item:
            return "任务不存在。"
        return (
            f"任务 ID：{item['id']}\n"
            f"账号：{item['session_label']}\n"
            f"群：{item['group_title']}\n"
            f"时间：{format_dt(item['schedule_at'], self.config.default_timezone)}\n"
            f"重复：{self.repeat_mode_text(item['repeat_mode'])}\n"
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

    def task_detail_keyboard(self, task_id: int, return_page: int = 0) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("停用任务", callback_data=f"task:delete:{task_id}:{max(0, return_page)}")],
                [InlineKeyboardButton("返回任务列表", callback_data=f"tasks:page:{max(0, return_page)}")],
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
            batch_id = job.get("batch_id")
            if batch_id:
                await self.refresh_join_batch_message(int(batch_id))
            session_row = self.db.get_session(job["session_id"])
            if not session_row:
                self.db.finish_join_job(job["id"], "failed", last_error="账号不存在")
                if batch_id:
                    await self.refresh_join_batch_message(int(batch_id))
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
                if batch_id:
                    await self.refresh_join_batch_message(int(batch_id))
            except Exception as exc:
                message = self.telethon.describe_error(exc)
                if "风控等待" in message:
                    retry_at = (datetime.utcnow() + timedelta(seconds=self.telethon.extract_wait_seconds(exc))).replace(microsecond=0).isoformat()
                    self.db.retry_join_job(job["id"], retry_at, message)
                    if batch_id:
                        await self.refresh_join_batch_message(int(batch_id))
                else:
                    self.db.finish_join_job(job["id"], "failed", last_error=message)
                    self.db.update_session(session_row["id"], status="offline" if message == "账号掉线" else session_row["status"], last_error=message)
                    if batch_id:
                        await self.refresh_join_batch_message(int(batch_id))
            await asyncio.sleep(1)

    async def repeat_worker(self) -> None:
        while True:
            self.prune_completed_once_tasks()
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
