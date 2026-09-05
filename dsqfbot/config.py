from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(slots=True)
class AppConfig:
    bot_token: str
    admin_ids: set[int]
    api_id: int
    api_hash: str
    database_path: Path
    session_dir: Path
    default_join_interval_seconds: int
    repeat_lookahead_minutes: int
    default_timezone: str

    def is_admin(self, user_id: int) -> bool:
        return not self.admin_ids or user_id in self.admin_ids


def _parse_admin_ids(value: str) -> set[int]:
    result: set[int] = set()
    for part in (value or "").split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


def load_config(base_dir: Path) -> AppConfig:
    load_dotenv(base_dir / ".env")
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    api_id = int(os.getenv("API_ID", "0") or "0")
    api_hash = os.getenv("API_HASH", "").strip()
    database_path = (base_dir / os.getenv("DATABASE_PATH", "storage/dsqfbot.sqlite3")).resolve()
    session_dir = (base_dir / os.getenv("SESSION_DIR", "storage/sessions")).resolve()
    return AppConfig(
        bot_token=bot_token,
        admin_ids=_parse_admin_ids(os.getenv("ADMIN_IDS", "")),
        api_id=api_id,
        api_hash=api_hash,
        database_path=database_path,
        session_dir=session_dir,
        default_join_interval_seconds=max(int(os.getenv("DEFAULT_JOIN_INTERVAL_SECONDS", "60") or "60"), 5),
        repeat_lookahead_minutes=max(int(os.getenv("REPEAT_LOOKAHEAD_MINUTES", "5") or "5"), 1),
        default_timezone=os.getenv("DEFAULT_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai",
    )
