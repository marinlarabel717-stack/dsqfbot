from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
