from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo

LINK_RE = re.compile(r"(https?://t\.me/[^\s]+|@[\w\d_]{4,}|t\.me/[^\s]+)", re.IGNORECASE)


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip())
    value = value.strip("-_").lower()
    return value or "session"


def parse_links(text: str) -> list[str]:
    seen: set[str] = set()
    items: list[str] = []
    for match in LINK_RE.findall(text or ""):
        link = normalize_link(match)
        if link not in seen:
            seen.add(link)
            items.append(link)
    return items


def normalize_link(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("@"):
        return value
    if value.startswith("t.me/"):
        return f"https://{value}"
    return value


def chunked(items: Iterable, size: int) -> list[list]:
    buffer = list(items)
    return [buffer[index:index + size] for index in range(0, len(buffer), size)]


def parse_user_datetime(value: str, timezone_name: str) -> datetime:
    value = (value or "").strip()
    dt = datetime.strptime(value, "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=ZoneInfo(timezone_name))


def format_dt(value: str | None, timezone_name: str) -> str:
    if not value:
        return "-"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M")
