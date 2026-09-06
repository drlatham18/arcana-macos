from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
RULES_GLOB = "MagicCompRules-*.txt"
INDEX_PATH = PROJECT_ROOT / ".cache" / "knowledge.sqlite3"

_RULE_LINE = re.compile(r"^(\d{3}(?:\.\d+[a-z]?)?)[.]?\s+(.+)$")
_WORD = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9'’-]*")
_lock = threading.Lock()


@dataclass(frozen=True)
class KnowledgeHit:
    title: str
    text: str
    source: str
    locator: str = ""

    @property
    def citation(self) -> str:
        return f"{self.source} · {self.locator}" if self.locator else self.source


def source_files() -> list[Path]:
    rules = sorted(PROJECT_ROOT.glob(RULES_GLOB), reverse=True)
    guides = sorted(KNOWLEDGE_DIR.rglob("*.md")) + sorted(KNOWLEDGE_DIR.rglob("*.txt"))
    return [path for path in rules + guides if path.is_file()]


def _fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        stat = path.stat()
        digest.update(str(path.relative_to(PROJECT_ROOT)).encode())
        digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()


def _rule_chunks(path: Path):
    current_id = ""
    current: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        match = _RULE_LINE.match(line)
        if match:
            if current:
                yield f"Rule {current_id}", " ".join(current), path.name, current_id
            current_id = match.group(1)
            current = [line]
        elif current:
            current.append(line)
    if current:
        yield f"Rule {current_id}", " ".join(current), path.name, current_id


def _markdown_chunks(path: Path):
    heading = path.stem.replace("-", " ").title()
    buffer: list[str] = []
    size = 0

    def flush():
        nonlocal buffer, size
        if not buffer:
            return None
        row = (heading, " ".join(buffer), path.name, heading)
        buffer, size = [], 0
        return row

    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if raw.startswith("#"):
            row = flush()
            if row:
                yield row
            heading = raw.lstrip("# ").strip()
            continue
        line = raw.strip().lstrip("-* ")
        if not line or raw.lstrip().startswith(("Source:", "Updated:")):
            continue
        # Web pages sometimes arrive as a single long extracted line. Split it
        # into useful retrieval passages instead of indexing one enormous blob.
        pieces = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", line)
        for piece in pieces:
            if size and size + len(piece) > 1200:
                row = flush()
                if row:
                    yield row
            buffer.append(piece)
            size += len(piece) + 1
    row = flush()
    if row:
        yield row


def iter_chunks(paths: list[Path] | None = None):
    for path in paths or source_files():
        if path.name.startswith("MagicCompRules-"):
            yield from _rule_chunks(path)
        else:
            yield from _markdown_chunks(path)


def build_index(force: bool = False) -> tuple[int, Path]:
    paths = source_files()
    if not paths:
        return 0, INDEX_PATH
    fingerprint = _fingerprint(paths)
    INDEX_PATH.parent.mkdir(exist_ok=True)
    with _lock:
        connection = sqlite3.connect(INDEX_PATH)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
            previous = connection.execute(
                "SELECT value FROM metadata WHERE key='fingerprint'"
            ).fetchone()
            if not force and previous and previous[0] == fingerprint:
                count = connection.execute("SELECT count(*) FROM knowledge").fetchone()[0]
                return int(count), INDEX_PATH
            connection.execute("DROP TABLE IF EXISTS knowledge")
            connection.execute(
                "CREATE VIRTUAL TABLE knowledge USING fts5(title, body, source, locator, tokenize='unicode61')"
            )
            rows = list(iter_chunks(paths))
            connection.executemany(
                "INSERT INTO knowledge(title, body, source, locator) VALUES (?, ?, ?, ?)", rows
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('fingerprint', ?)",
                (fingerprint,),
            )
            connection.commit()
            return len(rows), INDEX_PATH
        finally:
            connection.close()


def search_knowledge(query: str | list[str], limit: int = 5) -> list[KnowledgeHit]:
    if isinstance(query, str):
        number = query.strip().rstrip(".")
        if re.fullmatch(r"\d{3}(?:\.\d+[a-z]?)?", number):
            build_index()
            with sqlite3.connect(INDEX_PATH) as connection:
                rows = connection.execute(
                    "SELECT title, body, source, locator FROM knowledge WHERE locator = ? "
                    "OR locator LIKE ? ORDER BY CASE WHEN locator = ? THEN 0 ELSE 1 END, length(locator), locator LIMIT ?",
                    (number, number + "%", number, limit),
                ).fetchall()
            return [KnowledgeHit(*row) for row in rows]
    terms = query if isinstance(query, list) else _WORD.findall(query.casefold())
    terms = [term for term in terms if len(term) > 1]
    if not terms:
        return []
    build_index()
    # FTS syntax is deliberately generated rather than accepting raw user syntax.
    expression = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:16])
    with sqlite3.connect(INDEX_PATH) as connection:
        rows = connection.execute(
            "SELECT title, body, source, locator FROM knowledge "
            "WHERE knowledge MATCH ? ORDER BY bm25(knowledge, 4.0, 1.0, 0.3, 1.5) LIMIT ?",
            (expression, limit),
        ).fetchall()
    return [KnowledgeHit(*row) for row in rows]


def knowledge_status() -> str:
    count, _path = build_index()
    files = source_files()
    return f"{count:,} indexed passages from {len(files)} local sources"
