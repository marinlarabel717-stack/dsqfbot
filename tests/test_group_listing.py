from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dsqfbot.app import DsqfBotApp
from dsqfbot.config import AppConfig
from dsqfbot.db import Database


def make_config(base_dir: Path) -> AppConfig:
    return AppConfig(
        bot_token="token",
        admin_ids=set(),
        api_id=1,
        api_hash="hash",
        database_path=base_dir / "test.sqlite3",
        session_dir=base_dir / "sessions",
        client_device_model="DSQFBot",
        client_system_version="Linux",
        client_app_version="1.0",
        client_lang_code="zh-hans",
        client_system_lang_code="zh-hans",
        default_join_interval_seconds=60,
        repeat_lookahead_minutes=5,
        default_timezone="Asia/Shanghai",
        bot_concurrent_updates=1,
        telethon_timeout_seconds=20,
        log_level="INFO",
        log_file=base_dir / "bot.log",
        log_max_bytes=1024 * 1024,
        log_backup_count=1,
    )


class GroupListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        base_dir = Path(self.temp_dir.name)
        self.db = Database(base_dir / "test.sqlite3")
        self.app = DsqfBotApp(make_config(base_dir), self.db, telethon=object())
        self.session_id = self.db.create_session("test", "+10000000000", "test.session", False)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_groups_page_prefers_in_group_records(self) -> None:
        self.db.upsert_group(self.session_id, 101, "Joined Group", "joined_group", "https://t.me/joined_group")
        self.db.upsert_group(
            self.session_id,
            102,
            "Left Group",
            "left_group",
            "https://t.me/left_group",
            join_status="left",
            speak_status="同步后已移除",
        )

        groups, total, current_page, fallback_mode = self.app.group_page_items(self.session_id)

        self.assertEqual(total, 1)
        self.assertEqual(current_page, 0)
        self.assertFalse(fallback_mode)
        self.assertEqual([item["title"] for item in groups], ["Joined Group"])

    def test_groups_page_falls_back_to_recent_synced_groups(self) -> None:
        self.db.upsert_group(
            self.session_id,
            201,
            "Recently Synced A",
            "group_a",
            "https://t.me/group_a",
            join_status="left",
            speak_status="同步后已移除",
        )
        self.db.upsert_group(
            self.session_id,
            202,
            "Recently Synced B",
            "group_b",
            "https://t.me/group_b",
            join_status="left",
            speak_status="禁言",
        )

        text = self.app.groups_text(self.session_id)
        groups, total, _, fallback_mode = self.app.group_page_items(self.session_id)

        self.assertTrue(fallback_mode)
        self.assertEqual(total, 2)
        self.assertEqual([item["title"] for item in groups], ["Recently Synced A", "Recently Synced B"])
        self.assertIn("最近同步到的非频道记录", text)
        self.assertIn("Recently Synced A", text)
        self.assertIn("Recently Synced B", text)

    def test_sendable_groups_empty_text_explains_non_sendable_groups(self) -> None:
        self.db.upsert_group(
            self.session_id,
            301,
            "Muted Group",
            "muted_group",
            "https://t.me/muted_group",
            join_status="joined",
            speak_status="禁言",
        )

        text = self.app.sendable_groups_empty_text(self.session_id)

        self.assertIn("没有正常可发的在群群组", text)
        self.assertIn("当前在群：1", text)
        self.assertIn("正常可发：0", text)

    def test_sendable_groups_empty_text_explains_missing_joined_groups(self) -> None:
        self.db.upsert_group(
            self.session_id,
            302,
            "Left Group",
            "left_group",
            "https://t.me/left_group",
            join_status="left",
            speak_status="同步后已移除",
        )

        text = self.app.sendable_groups_empty_text(self.session_id)

        self.assertIn("没有正常可发的在群群组", text)
        self.assertIn("没有判定为“在群”的群", text)


    def test_schedule_candidate_groups_include_joined_unknown_groups(self) -> None:
        self.db.upsert_group(
            self.session_id,
            401,
            "Unchecked Group",
            "unchecked_group",
            "https://t.me/unchecked_group",
        )
        self.db.upsert_group(
            self.session_id,
            402,
            "Channel Record",
            "channel_record",
            "https://t.me/channel_record",
            is_channel=True,
            speak_status="频道跳过",
        )
        self.db.upsert_group(
            self.session_id,
            403,
            "Left Group",
            "left_group_2",
            "https://t.me/left_group_2",
            join_status="left",
            speak_status="同步后已移除",
        )

        groups = self.app.schedule_candidate_groups(self.session_id)

        self.assertEqual([item["title"] for item in groups], ["Unchecked Group"])

    def test_schedule_candidate_groups_include_joined_non_sendable_groups(self) -> None:
        self.db.upsert_group(
            self.session_id,
            404,
            "Muted Group",
            "muted_group_2",
            "https://t.me/muted_group_2",
            join_status="joined",
            speak_status="禁言",
        )

        groups = self.app.schedule_candidate_groups(self.session_id)

        self.assertEqual([item["title"] for item in groups], ["Muted Group"])

    def test_orphan_tasks_are_not_counted_or_listed(self) -> None:
        with self.db.connect() as conn:
            conn.execute("PRAGMA foreign_keys=OFF;")
            conn.execute(
                """
                INSERT INTO tasks (
                    session_id, group_id, message_text, schedule_at, repeat_mode,
                    next_run_at, last_scheduled_for, last_telegram_message_id,
                    status, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.session_id,
                    999999,
                    "stale task",
                    "2999-01-01T00:00:00+08:00",
                    "once",
                    None,
                    None,
                    None,
                    "scheduled",
                    None,
                    "2026-01-01T00:00:00+08:00",
                    "2026-01-01T00:00:00+08:00",
                ),
            )
            conn.commit()

        self.assertEqual(self.db.count_tasks(), 1)
        self.assertEqual(self.db.count_active_tasks(self.app.active_task_cutoff_iso()), 0)

        tasks, total, current_page = self.app.task_page_items()

        self.assertEqual(tasks, [])
        self.assertEqual(total, 0)
        self.assertEqual(current_page, 0)
        self.assertEqual(self.db.count_tasks(), 0)

    def test_once_tasks_are_completed_but_not_deleted_after_send_time(self) -> None:
        group_id = self.db.upsert_group(self.session_id, 501, "Realtime Group", "realtime_group", "https://t.me/realtime_group")
        now = datetime.now(tz=ZoneInfo(self.app.config.default_timezone))
        task_id = self.db.create_task(
            session_id=self.session_id,
            group_id=group_id,
            message_text="hello",
            schedule_at=(now - timedelta(minutes=2)).isoformat(),
            repeat_mode="once",
            next_run_at=None,
            last_scheduled_for=(now - timedelta(minutes=2)).isoformat(),
            last_telegram_message_id=12345,
            status="scheduled",
        )

        removed = self.app.prune_stale_task_records()
        task = self.db.get_task(task_id)

        self.assertEqual(removed, 1)
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(self.db.count_active_tasks(self.app.active_task_cutoff_iso()), 0)
        self.assertIn("状态：已发出", self.app.task_detail_text(task_id))

    def test_tasks_text_explains_realtime_count_for_once_tasks(self) -> None:
        group_id = self.db.upsert_group(self.session_id, 502, "Future Group", "future_group", "https://t.me/future_group")
        now = datetime.now(tz=ZoneInfo(self.app.config.default_timezone))
        self.db.create_task(
            session_id=self.session_id,
            group_id=group_id,
            message_text="hello future",
            schedule_at=(now + timedelta(minutes=30)).isoformat(),
            repeat_mode="once",
            next_run_at=None,
            last_scheduled_for=(now + timedelta(minutes=30)).isoformat(),
            last_telegram_message_id=67890,
            status="scheduled",
        )

        text = self.app.tasks_text()

        self.assertIn("点下方“查看任务”后，把任务编号发给我。", text)
        self.assertIn("单次任务发完后会自动扣减；上面显示的是实时有效条数。", text)


if __name__ == "__main__":
    unittest.main()
