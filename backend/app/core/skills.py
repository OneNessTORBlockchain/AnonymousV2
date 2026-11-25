"""Agent-authored skills: create once, install into the sandbox, reuse forever.

Every model in the fleet may write a small program (python, bash or node),
register it here, and from then on call it as a single tool invocation instead
of re-deriving the same pipeline.  A skill lives on the host under
``storage/skills/<slug>/`` and is installed into the Kali workdir at
``<kali_workdir>/skills/<slug>/`` the first time it runs.

The vault is deliberately small and dependency-free: a JSON manifest per skill
plus one source file, so an operator can read, edit or delete a skill by hand.
"""
from __future__ import annotations

import json
import re
import shlex
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: language -> (file extension, interpreter inside the container)
LANGUAGES: dict[str, tuple[str, str]] = {
    "python": (".py", "python3"),
    "py": (".py", "python3"),
    "python3": (".py", "python3"),
    "bash": (".sh", "bash"),
    "sh": (".sh", "bash"),
    "shell": (".sh", "bash"),
    "node": (".js", "node"),
    "javascript": (".js", "node"),
    "js": (".js", "node"),
}
#: Source is capped: a skill is a tool, not a repository.
MAX_CODE_BYTES = 200_000
_SAFE = re.compile(r"[^a-z0-9._-]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str) -> str:
    """``"Parse Nessus XML!"`` -> ``parse-nessus-xml``."""
    text = _SAFE.sub("-", str(value or "").strip().lower()).strip("-._")
    text = re.sub(r"-{2,}", "-", text)
    return text[:60] or "skill"


def clean_language(value: str) -> str:
    language = str(value or "python").strip().lower()
    return language if language in LANGUAGES else "python"


class SkillVault:
    """Host-side registry of agent skills plus the sandbox installer."""

    def __init__(self, settings: Any, sandbox: Any = None) -> None:
        self.settings = settings
        self.sandbox = sandbox
        self.root = Path(settings.skills_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # -- paths ------------------------------------------------------------
    @property
    def container_root(self) -> str:
        base = str(self.settings.kali_workdir or "/work").rstrip("/") or "/work"
        return f"{base}/skills"

    def container_dir(self, slug: str) -> str:
        return f"{self.container_root}/{slug}"

    def skill_dir(self, slug: str) -> Path:
        return self.root / slugify(slug)

    # -- persistence ------------------------------------------------------
    @staticmethod
    def _load(directory: Path) -> dict[str, Any] | None:
        manifest = directory / "skill.json"
        if not manifest.is_file():
            return None
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        payload.setdefault("slug", directory.name)
        payload.setdefault("name", payload["slug"])
        payload.setdefault("language", "python")
        payload.setdefault("tags", [])
        payload.setdefault("runs", 0)
        return payload

    def _entries(self) -> list[dict[str, Any]]:
        skills: list[dict[str, Any]] = []
        for directory in sorted(self.root.iterdir() if self.root.is_dir() else []):
            if not directory.is_dir():
                continue
            record = self._load(directory)
            if record is not None:
                skills.append(record)
        skills.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return skills

    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._entries()[: max(1, min(int(limit or 200), 500))]

    def resolve(self, name: str) -> dict[str, Any] | None:
        """Find a skill by slug or by its human name (case-insensitive)."""
        wanted = slugify(name)
        for record in self._entries():
            if str(record.get("slug")) == wanted:
                return record
            if slugify(str(record.get("name") or "")) == wanted:
                return record
        return None

    def get(self, name: str) -> dict[str, Any] | None:
        record = self.resolve(name)
        if record is None:
            return None
        source = self.skill_dir(str(record["slug"])) / str(record.get("entry") or "main.py")
        try:
            record["code"] = source.read_text(encoding="utf-8")
        except OSError:
            record["code"] = ""
        return record

    # -- create / edit / delete -------------------------------------------
    def create(
        self,
        *,
        name: str,
        description: str = "",
        code: str = "",
        language: str = "python",
        entry: str = "",
        author: str = "",
        model: str = "",
        tags: Any = None,
    ) -> dict[str, Any]:
        """Register a new skill (or refresh the source of an existing one)."""
        label = str(name or "").strip()
        if not label:
            raise ValueError("skill name must not be blank")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("skill code must not be blank")
        if len(code.encode("utf-8", "replace")) > MAX_CODE_BYTES:
            raise ValueError(f"skill code exceeds {MAX_CODE_BYTES} bytes")
        language_key = clean_language(language)
        extension, _interpreter = LANGUAGES[language_key]
        slug = slugify(label)
        directory = self.skill_dir(slug)
        directory.mkdir(parents=True, exist_ok=True)
        entry_name = Path(str(entry or "")).name or f"main{extension}"
        if not entry_name.endswith(extension):
            entry_name = f"{Path(entry_name).stem or 'main'}{extension}"
        source = directory / entry_name
        source.write_text(code, encoding="utf-8")
        now = utc_now()
        existing = self._load(directory) or {}
        if isinstance(tags, str):
            tag_list = [tag.strip() for tag in re.split(r"[,;]+", tags) if tag.strip()]
        elif isinstance(tags, (list, tuple, set)):
            tag_list = [str(tag).strip() for tag in tags if str(tag).strip()]
        else:
            tag_list = list(existing.get("tags") or [])
        manifest = {
            "slug": slug,
            "name": label[:120],
            "description": str(description or existing.get("description") or "").strip()[:600],
            "language": language_key,
            "entry": entry_name,
            "tags": tag_list[:20],
            "author": str(author or existing.get("author") or "")[:120],
            "model": str(model or existing.get("model") or "")[:160],
            "created_at": str(existing.get("created_at") or now),
            "updated_at": now,
            "runs": int(existing.get("runs") or 0),
            "last_run": str(existing.get("last_run") or ""),
            "last_ok": bool(existing.get("last_ok")) if "last_ok" in existing else False,
            "run_command": f"{_interpreter} {self.container_dir(slug)}/{entry_name}",
        }
        (directory / "skill.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        manifest["code"] = code
        manifest["created"] = not bool(existing)
        return manifest

    def delete(self, name: str) -> bool:
        record = self.resolve(name)
        if record is None:
            return False
        directory = self.skill_dir(str(record["slug"]))
        removed = False
        if directory.is_dir():
            for item in sorted(directory.rglob("*"), reverse=True):
                try:
                    item.unlink() if item.is_file() else item.rmdir()
                    removed = True
                except OSError:
                    continue
            try:
                directory.rmdir()
                removed = True
            except OSError:
                pass
        return removed

    def _record_run(self, record: dict[str, Any], ok: bool) -> None:
        directory = self.skill_dir(str(record.get("slug") or ""))
        manifest = self._load(directory)
        if manifest is None:
            return
        manifest["runs"] = int(manifest.get("runs") or 0) + 1
        manifest["last_run"] = utc_now()
        manifest["last_ok"] = bool(ok)
        manifest.pop("code", None)
        try:
            (directory / "skill.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass


    # -- install / run ----------------------------------------------------
    async def install(self, name: str, *, workdir: str | None = None) -> dict[str, Any]:
        """Copy the skill into the Kali workdir so every agent can run it."""
        record = self.get(name)
        if record is None:
            return {"ok": False, "content": f"no such skill: {name}", "data": {}}
        if self.sandbox is None:
            return {"ok": False, "content": "skill vault has no sandbox attached", "data": {}}
        slug = str(record["slug"])
        entry_name = str(record.get("entry") or "main.py")
        target_dir = self.container_dir(slug)
        written = await self.sandbox.write_file(
            f"{target_dir}/{entry_name}", str(record.get("code") or ""), cwd=workdir or self.settings.kali_workdir
        )
        if not written.ok:
            return {
                "ok": False,
                "content": f"could not install skill {slug}: {written.stderr or written.stdout}",
                "data": {"slug": slug},
            }
        marker = json.dumps(
            {key: record.get(key) for key in ("slug", "name", "description", "language", "entry", "tags")},
            indent=2,
        )
        await self.sandbox.write_file(f"{target_dir}/skill.json", marker, cwd=workdir or self.settings.kali_workdir)
        chmod = await self.sandbox.exec(
            f"chmod +x {shlex.quote(f'{target_dir}/{entry_name}')}", timeout=30
        )
        return {
            "ok": True,
            "content": f"installed {slug} -> {target_dir}/{entry_name}",
            "data": {"slug": slug, "path": f"{target_dir}/{entry_name}", "chmod": chmod.ok},
        }

    async def run(
        self,
        name: str,
        *,
        args: str = "",
        timeout: int = 300,
        workdir: str | None = None,
    ) -> dict[str, Any]:
        """Install (if needed) and execute one skill inside the container."""
        record = self.get(name)
        if record is None:
            return {"ok": False, "content": f"no such skill: {name}", "data": {}}
        installed = await self.install(str(record["slug"]), workdir=workdir)
        if not installed.get("ok"):
            self._record_run(record, False)
            return installed
        slug = str(record["slug"])
        entry_name = str(record.get("entry") or "main.py")
        _extension, interpreter = LANGUAGES[str(record.get("language") or "python")]
        command = f"{interpreter} {shlex.quote(f'{self.container_dir(slug)}/{entry_name}')}"
        extra = str(args or "").strip()
        if extra:
            command = f"{command} {extra}"
        result = await self.sandbox.exec(command, timeout=timeout, cwd=workdir)
        self._record_run(record, bool(result.ok))
        return {
            "ok": bool(result.ok),
            "content": result.for_model(),
            "data": {
                "slug": slug,
                "command": command,
                "exit_code": result.exit_code,
                "path": f"{self.container_dir(slug)}/{entry_name}",
                "result": result.to_dict(),
            },
        }

    # -- prompt material ---------------------------------------------------
    def manual(self, limit: int = 10) -> str:
        """One line per installed skill, for the agent system prompt."""
        records = self.list(limit)
        if not records:
            return ""
        lines: list[str] = []
        for record in records:
            description = str(record.get("description") or "").strip()[:160]
            lines.append(
                f"- {record.get('slug')} ({record.get('language')}, {record.get('runs', 0)} runs)"
                + (f": {description}" if description else "")
            )
        return "\n".join(lines)


__all__ = ["LANGUAGES", "MAX_CODE_BYTES", "SkillVault", "clean_language", "slugify"]

