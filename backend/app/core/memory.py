"""Durable shared knowledge base for every agent in Anonymous V2.

The swarm already has a per-run blackboard (:class:`app.core.swarm.SwarmMemory`)
that dies with the run.  This module is the long-term memory that survives
runs, restarts and model swaps:

``technique``
    how something was done (payload shape, encoder, bypass, chaining).
``tool``
    a tool that turned out to be installed, useful, or missing, plus the exact
    invocation that worked.
``skill``
    a reusable script/tool an agent wrote, and how to run it.
``error``
    an error that must not be repeated, with the fix.
``strategy``
    a planning idea that paid off (or failed) for this class of objective.
``lesson``
    anything else worth remembering.

Storage is a small SQLite database (stdlib ``sqlite3``) at
``settings.memory_path`` so the operator and every agent can read and write it
concurrently.  Agents write through ``memory_write`` after each task (enforced
by the swarm runner) and read a compact digest before each task, which is what
turns a fleet into something that actually learns.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

#: Note kinds an agent may write.  ``all`` is a read-only filter.
KINDS: tuple[str, ...] = ("technique", "tool", "skill", "error", "strategy", "lesson")
#: Order used when a digest is rendered: hazards first, then plans, then craft.
_DIGEST_ORDER = ("error", "strategy", "technique", "tool", "skill", "lesson")
_MAX_TITLE = 200
_MAX_BODY = 8_000
_MAX_TAGS = 400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    body        TEXT    NOT NULL DEFAULT '',
    tags        TEXT    NOT NULL DEFAULT '',
    source      TEXT    NOT NULL DEFAULT '',
    agent       TEXT    NOT NULL DEFAULT '',
    model       TEXT    NOT NULL DEFAULT '',
    session_id  TEXT    NOT NULL DEFAULT '',
    run_id      TEXT    NOT NULL DEFAULT '',
    task        TEXT    NOT NULL DEFAULT '',
    outcome     TEXT    NOT NULL DEFAULT '',
    confidence  REAL    NOT NULL DEFAULT 0.6,
    uses        INTEGER NOT NULL DEFAULT 0,
    helpful     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_kind_idx ON notes(kind);
CREATE INDEX IF NOT EXISTS notes_updated_idx ON notes(updated_at DESC);
"""


def utc_now() -> str:
    """Timestamp used by every record in the knowledge base."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _tags(value: Iterable[str] | str | None) -> str:
    if isinstance(value, str):
        parts = re.split(r"[,;\s]+", value)
    elif value is None:
        parts = []
    else:
        parts = [str(item) for item in value]
    seen: list[str] = []
    for part in parts:
        tag = re.sub(r"[^A-Za-z0-9._+-]+", "-", part.strip().lower()).strip("-")
        if tag and tag not in seen:
            seen.append(tag)
    return ",".join(seen)[:_MAX_TAGS]


class KnowledgeBase:
    """SQLite-backed long-term memory shared by every agent and the operator."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._ready = False

    # -- plumbing ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _ensure(self, conn: sqlite3.Connection) -> None:
        if self._ready:
            return
        conn.executescript(_SCHEMA)
        self._ready = True

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row)
        record["tags"] = [tag for tag in str(record.get("tags") or "").split(",") if tag]
        return record

    # -- writes -----------------------------------------------------------
    def write(
        self,
        *,
        kind: str,
        title: str,
        body: str = "",
        tags: Iterable[str] | str | None = None,
        source: str = "",
        agent: str = "",
        model: str = "",
        session_id: str = "",
        run_id: str = "",
        task: str = "",
        outcome: str = "",
        confidence: float = 0.6,
    ) -> dict[str, Any]:
        """Insert one note, or refresh the note that already carries this title.

        Re-writing the same lesson (a second agent, a second run) must sharpen
        it instead of filling the database with duplicates, so ``kind`` plus the
        normalised ``title`` is the natural key: the body is replaced when the
        new one is longer, and confidence is raised.
        """
        clean_kind = _clean(kind, 20).lower()
        if clean_kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        clean_title = _clean(title, _MAX_TITLE)
        if not clean_title:
            raise ValueError("title must not be blank")
        clean_body = _clean(body, _MAX_BODY)
        clean_tags = _tags(tags)
        now = utc_now()
        try:
            confidence_value = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence_value = 0.6

        with self._lock:
            conn = self._connect()
            try:
                self._ensure(conn)
                existing = conn.execute(
                    "SELECT * FROM notes WHERE kind = ? AND lower(title) = ? ORDER BY id LIMIT 1",
                    (clean_kind, _norm(clean_title)),
                ).fetchone()
                if existing is not None:
                    keep_body = (
                        existing["body"]
                        if len(existing["body"] or "") >= len(clean_body)
                        else clean_body
                    )
                    conn.execute(
                        "UPDATE notes SET body = ?, tags = ?, confidence = MAX(confidence, ?), "
                        "updated_at = ?, agent = COALESCE(NULLIF(agent, ''), ?), "
                        "model = COALESCE(NULLIF(model, ''), ?), "
                        "outcome = COALESCE(NULLIF(?, ''), outcome) WHERE id = ?",
                        (
                            keep_body,
                            clean_tags or existing["tags"],
                            confidence_value,
                            now,
                            _clean(agent, 80),
                            _clean(model, 120),
                            _clean(outcome, 40),
                            existing["id"],
                        ),
                    )
                    conn.commit()
                    record = self._row(
                        conn.execute("SELECT * FROM notes WHERE id = ?", (existing["id"],)).fetchone()
                    )
                    assert record is not None
                    record["updated"] = True
                    return record
                cursor = conn.execute(
                    "INSERT INTO notes (kind, title, body, tags, source, agent, model, session_id, "
                    "run_id, task, outcome, confidence, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        clean_kind,
                        clean_title,
                        clean_body,
                        clean_tags,
                        _clean(source, 120),
                        _clean(agent, 80),
                        _clean(model, 120),
                        _clean(session_id, 120),
                        _clean(run_id, 80),
                        _clean(task, 2_000),
                        _clean(outcome, 40),
                        confidence_value,
                        now,
                        now,
                    ),
                )
                conn.commit()
                record = self._row(
                    conn.execute("SELECT * FROM notes WHERE id = ?", (cursor.lastrowid,)).fetchone()
                )
                assert record is not None
                record["updated"] = False
                return record
            finally:
                conn.close()

    def write_many(
        self, notes: Iterable[dict[str, Any]], /, **defaults: Any
    ) -> list[dict[str, Any]]:
        """Write several notes, silently skipping the blank ones."""
        written: list[dict[str, Any]] = []
        for note in notes:
            if not isinstance(note, dict):
                continue
            payload = {**defaults, **note}
            if not str(payload.get("title") or "").strip():
                continue
            try:
                written.append(self.write(**payload))
            except (ValueError, sqlite3.Error, TypeError):
                continue
        return written

    def update(self, note_id: int, **fields: Any) -> dict[str, Any] | None:
        """Edit a note (operator review) and bump ``updated_at``."""
        allowed = {"kind", "title", "body", "tags", "confidence", "outcome", "helpful"}
        changes: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            if key == "kind":
                clean = _clean(value, 20).lower()
                if clean not in KINDS:
                    continue
                changes[key] = clean
            elif key == "tags":
                changes[key] = _tags(value)
            elif key == "confidence":
                try:
                    changes[key] = max(0.0, min(1.0, float(value)))
                except (TypeError, ValueError):
                    continue
            elif key == "helpful":
                changes[key] = 1 if value else 0
            elif key == "title":
                clean = _clean(value, _MAX_TITLE)
                if clean:
                    changes[key] = clean
            elif key == "body":
                changes[key] = _clean(value, _MAX_BODY)
            else:
                changes[key] = _clean(value, 40)
        if not changes:
            return self.get(note_id)
        changes["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in changes)
        with self._lock:
            conn = self._connect()
            try:
                self._ensure(conn)
                conn.execute(
                    f"UPDATE notes SET {assignments} WHERE id = ?",  # noqa: S608 - whitelisted keys
                    (*changes.values(), int(note_id)),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get(note_id)

    def delete(self, note_id: int) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                self._ensure(conn)
                cursor = conn.execute("DELETE FROM notes WHERE id = ?", (int(note_id),))
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    def mark_used(self, note_ids: Iterable[int]) -> int:
        """Count how often a lesson was actually consulted (learning signal)."""
        ids = [int(value) for value in note_ids if str(value).isdigit()]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            conn = self._connect()
            try:
                self._ensure(conn)
                cursor = conn.execute(
                    f"UPDATE notes SET uses = uses + 1 WHERE id IN ({placeholders})",  # noqa: S608
                    tuple(ids),
                )
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()


    # -- reads ------------------------------------------------------------
    def get(self, note_id: int) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            self._ensure(conn)
            return self._row(
                conn.execute("SELECT * FROM notes WHERE id = ?", (int(note_id),)).fetchone()
            )
        finally:
            conn.close()

    def search(
        self,
        query: str = "",
        *,
        kind: str = "",
        tag: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Newest-first notes matching an optional free-text / kind / tag filter."""
        clauses: list[str] = []
        params: list[Any] = []
        clean_kind = _clean(kind, 20).lower()
        if clean_kind and clean_kind != "all":
            clauses.append("kind = ?")
            params.append(clean_kind)
        text = _clean(query, 200)
        if text:
            needle = f"%{text.lower()}%"
            clauses.append("(lower(title) LIKE ? OR lower(body) LIKE ? OR lower(tags) LIKE ?)")
            params.extend([needle, needle, needle])
        clean_tag = _clean(tag, 60).lower()
        if clean_tag:
            clauses.append("(',' || lower(tags) || ',') LIKE ?")
            params.append(f"%,{clean_tag},%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cap = max(1, min(int(limit or 20), 500))
        conn = self._connect()
        try:
            self._ensure(conn)
            rows = conn.execute(
                f"SELECT * FROM notes {where} ORDER BY updated_at DESC, id DESC LIMIT ?",  # noqa: S608
                (*params, cap),
            ).fetchall()
            collected: list[dict[str, Any]] = []
            for row in rows:
                record = self._row(row)
                if record is not None:
                    collected.append(record)
            return collected
        finally:
            conn.close()

    def counts(self) -> dict[str, int]:
        conn = self._connect()
        try:
            self._ensure(conn)
            rows = conn.execute("SELECT kind, COUNT(*) AS total FROM notes GROUP BY kind").fetchall()
            return {str(row["kind"]): int(row["total"]) for row in rows}
        finally:
            conn.close()

    def stats(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            self._ensure(conn)
            total = int(conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
            used = int(conn.execute("SELECT COALESCE(SUM(uses), 0) FROM notes").fetchone()[0])
            latest = conn.execute("SELECT MAX(updated_at) FROM notes").fetchone()[0]
            return {
                "total": total,
                "uses": used,
                "latest": latest or "",
                "kinds": self.counts(),
                "path": str(self.path),
            }
        finally:
            conn.close()



    # -- prompt material ---------------------------------------------------
    def digest(self, *, limit: int = 12, chars: int = 4_000, query: str = "") -> str:
        """Compact block injected into prompts: what the fleet already learned."""
        notes: list[dict[str, Any]] = []
        cap = max(1, min(int(limit or 12), 60))
        if query:
            notes = self.search(query, limit=cap)
        else:
            share = max(2, cap // len(_DIGEST_ORDER))
            for kind in _DIGEST_ORDER:
                notes.extend(self.search(kind=kind, limit=share))
        if not notes:
            return ""
        rank = {kind: index for index, kind in enumerate(_DIGEST_ORDER)}
        notes.sort(key=lambda item: rank.get(str(item.get("kind")), 99))
        lines: list[str] = []
        seen: set[str] = set()
        for note in notes:
            key = f"{note.get('kind')}|{_norm(str(note.get('title') or ''))}"
            if key in seen:
                continue
            seen.add(key)
            body = re.sub(r"\s+", " ", str(note.get("body") or ""))[:220]
            tags = note.get("tags") or []
            suffix = f" [{', '.join(tags[:4])}]" if tags else ""
            lines.append(f"- ({note.get('kind')}) {note.get('title')} :: {body}{suffix}")
        text = "\n".join(lines[: max(1, cap * 2)])
        if len(text) > chars:
            text = text[:chars].rsplit("\n", 1)[0] + "\n..."
        return text


__all__ = ["KINDS", "KnowledgeBase", "utc_now"]
