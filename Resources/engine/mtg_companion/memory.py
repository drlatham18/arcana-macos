from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MEMORY_PATH = ROOT / "data" / "companion-memory.sqlite3"
_WORDS = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9'’-]*")


@dataclass(frozen=True)
class MemoryHit:
    note: str
    context: str
    created_at: str


def _connect() -> sqlite3.Connection:
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(MEMORY_PATH)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS observations ("
        "signature TEXT PRIMARY KEY, seen_count INTEGER NOT NULL, last_seen TEXT NOT NULL, "
        "heading TEXT NOT NULL, screen_text TEXT NOT NULL, log_text TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS notes USING fts5("
        "note, context, created_at UNINDEXED, tokenize='unicode61')"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS screen_profiles ("
        "signature TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, "
        "seen_count INTEGER NOT NULL, window_name TEXT NOT NULL, popup_name TEXT NOT NULL, "
        "controls_json TEXT NOT NULL)"
    )
    return connection


def remember_observation(heading: str, screen_text: str, log_text: str = "") -> None:
    evidence = f"{screen_text.strip()}\n{log_text.strip()}"[:8000]
    if not evidence.strip():
        return
    signature = hashlib.sha256(f"{heading}\n{evidence.casefold()}".encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    with closing(_connect()) as connection:
        with connection:
            connection.execute(
                "INSERT INTO observations(signature, seen_count, last_seen, heading, screen_text, log_text) "
                "VALUES (?, 1, ?, ?, ?, ?) ON CONFLICT(signature) DO UPDATE SET "
                "seen_count=seen_count+1, last_seen=excluded.last_seen",
                (signature, now, heading, screen_text[:5000], log_text[:3000]),
            )


def teach(note: str, context: str = "") -> None:
    note = " ".join(note.split())
    context = " ".join(context.split())[:4000]
    if not note:
        raise ValueError("A correction cannot be empty.")
    with closing(_connect()) as connection:
        with connection:
            connection.execute(
                "INSERT INTO notes(note, context, created_at) VALUES (?, ?, ?)",
                (note[:2000], context, datetime.now(timezone.utc).isoformat()),
            )


def recall(query: str, limit: int = 3) -> list[MemoryHit]:
    terms = [word.casefold() for word in _WORDS.findall(query) if len(word) > 2]
    if not terms or not MEMORY_PATH.exists():
        return []
    expression = " OR ".join(f'"{term}"' for term in terms[:12])
    with closing(_connect()) as connection:
        rows = connection.execute(
            "SELECT note, context, created_at FROM notes WHERE notes MATCH ? "
            "ORDER BY bm25(notes, 3.0, 0.5, 0.0) LIMIT ?",
            (expression, limit),
        ).fetchall()
    return [MemoryHit(*row) for row in rows]


def memory_stats() -> tuple[int, int]:
    with closing(_connect()) as connection:
        observations = connection.execute("SELECT count(*) FROM observations").fetchone()[0]
        notes = connection.execute("SELECT count(*) FROM notes").fetchone()[0]
    return int(observations), int(notes)


def remember_screen_profile(window_name: str, popup_name: str, controls: list[dict[str, object]]) -> str:
    structural_types = {
        "Window", "Button", "Tab", "TabItem", "ComboBox", "List", "ListItem",
        "CheckBox", "RadioButton", "Edit", "Menu", "MenuItem", "Tree", "TreeItem",
    }
    structural = [
        {
            "type": item.get("control_type", ""),
            "name": item.get("name", ""),
            "id": item.get("automation_id", ""),
            "enabled": bool(item.get("enabled", False)),
            "visible": bool(item.get("visible", False)),
        }
        for item in controls
        if item.get("visible")
        and item.get("control_type") in structural_types
        and (item.get("name") or item.get("automation_id"))
    ]
    serialized = json.dumps(structural, ensure_ascii=False, sort_keys=True)
    signature = hashlib.sha256(
        f"{window_name}\n{popup_name}\n{serialized}".encode("utf-8")
    ).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    with closing(_connect()) as connection:
        with connection:
            connection.execute(
                "INSERT INTO screen_profiles(signature, first_seen, last_seen, seen_count, window_name, popup_name, controls_json) "
                "VALUES (?, ?, ?, 1, ?, ?, ?) ON CONFLICT(signature) DO UPDATE SET "
                "last_seen=excluded.last_seen, seen_count=seen_count+1",
                (signature, now, now, window_name, popup_name, serialized),
            )
    return signature


def export_screen_profiles() -> tuple[Path, Path]:
    """Export every learned MTGO structure for humans and offline tooling."""
    json_path = ROOT / "knowledge" / "mtgo-learned-screens.json"
    markdown_path = ROOT / "knowledge" / "mtgo-learned-screens.md"
    with closing(_connect()) as connection:
        rows = connection.execute(
            "SELECT signature, first_seen, last_seen, seen_count, window_name, popup_name, controls_json "
            "FROM screen_profiles ORDER BY last_seen DESC"
        ).fetchall()
    screens = [
        {
            "signature": row[0],
            "first_seen": row[1],
            "last_seen": row[2],
            "seen_count": row[3],
            "window_name": row[4],
            "popup_name": row[5],
            "controls": json.loads(row[6]),
        }
        for row in rows
    ]
    payload = {
        "purpose": "Local MTGO page and popup structures learned without focus or mouse input.",
        "safety": "No purchase, trade, delete, concede, payment, or event-entry controls are activated.",
        "screens": screens,
    }
    lines = [
        "# MTGO learned screens",
        "",
        "This file grows automatically as MTGO pages and popups are opened. The companion reads controls through Windows UI Automation without taking focus.",
        "",
        "Purchases, trades, deletion, concession, payment, and event-entry confirmation are never automated.",
        "",
    ]
    for screen in screens:
        name = screen["popup_name"] or screen["window_name"] or "Unnamed MTGO screen"
        lines.extend([f"## {name}", "", f"Seen {screen['seen_count']} time(s); last seen {screen['last_seen']}.", ""])
        for control in screen["controls"]:
            label = control["name"] or control["id"]
            if not label or label.casefold() in {"this", "custom"}:
                continue
            identity = f" (`{control['id']}`)" if control["id"] else ""
            disabled = " — disabled" if not control["enabled"] else ""
            lines.append(f"- {control['type']}: {label}{identity}{disabled}")
        lines.append("")
    for path, content in (
        (json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"),
        (markdown_path, "\n".join(lines)),
    ):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    return json_path, markdown_path
