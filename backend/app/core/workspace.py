"""Operator <-> sandbox file exchange for Anonymous V2.

The operator can hand the fleet files of any format (single files or whole
folders).  Uploads are streamed to ``storage/uploads/<batch>`` on the host and
then copied into the Kali workdir with ``docker cp``; downloads are pulled back
and served as a single file or a zip of the container tree.  That closes the
loop: upload, agents create/edit folders, run and debug the app, then the
operator downloads the artefacts from the Workspace panel.

Only the configured Kali workdir (``/work`` by default) is reachable: every
path is normalised and rejected when it escapes that root.
"""
from __future__ import annotations

import asyncio
import mimetypes
import posixpath
import re
import shlex
import shutil
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator

from ..config import Settings
from .sandbox import Sandbox

#: Characters allowed in a path segment; everything else becomes ``_``.
_UNSAFE = re.compile(r"[^A-Za-z0-9._ +()\[\]{}@,-]+")
_MAX_REL_DEPTH = 10


def safe_segment(value: str, *, limit: int = 120) -> str:
    """One path component, safe for a host file name and a shell argument."""
    text = _UNSAFE.sub("_", str(value or "").strip())
    text = text.strip(" .") or "file"
    return text[:limit]


def safe_relative(value: str) -> str:
    """Normalise a browser-supplied relative path (``folder/sub/file.ext``)."""
    raw = str(value or "").replace("\\", "/")
    parts: list[str] = []
    for part in PurePosixPath(raw).parts:
        if part in ("", ".", "/"):
            continue
        if part == "..":
            continue
        parts.append(safe_segment(part))
    return "/".join(parts[:_MAX_REL_DEPTH]) or "file"


def human_bytes(size: int) -> str:
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


class FileExchange:
    """Host staging + ``docker cp`` bridge for the operator Workspace panel."""

    def __init__(self, settings: Settings, sandbox: Sandbox) -> None:
        self.settings = settings
        self.sandbox = sandbox
        self.root = Path(settings.uploads_dir)

    # -- path rules -------------------------------------------------------
    @property
    def container_root(self) -> str:
        return self.settings.kali_workdir.rstrip("/") or "/work"

    def batch_id(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def local_batch_dir(self, batch: str) -> Path:
        path = self.root / safe_segment(batch, limit=64)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_container_path(self, path: str = "", *, default: str | None = None) -> str:
        """Normalise ``path`` and refuse anything outside the Kali workdir."""
        root = self.container_root
        raw = str(path or default or root).strip().replace("\\", "/")
        if not raw.startswith("/"):
            raw = f"{root}/{raw.lstrip('/')}"
        resolved = posixpath.normpath(raw)
        if resolved == "/":
            resolved = root
        if not (resolved == root or resolved.startswith(root + "/")):
            raise ValueError(f"path must stay inside {root}")
        return resolved

    # -- upload -----------------------------------------------------------
    async def receive(
        self,
        *,
        batch: str,
        name: str,
        rel: str,
        chunks: AsyncIterator[bytes],
    ) -> dict[str, Any]:
        """Stream one uploaded file to ``storage/uploads/<batch>/<rel>``."""
        clean_name = safe_segment(name, limit=160)
        rel_path = safe_relative(rel) if rel else clean_name
        if not rel_path.endswith(clean_name):
            rel_path = clean_name if rel_path == "file" else safe_relative(f"{rel_path}/{clean_name}")
        batch_dir = self.local_batch_dir(batch).resolve()
        target = (batch_dir / rel_path).resolve()
        if self.root.resolve() not in target.parents:
            raise ValueError("upload path escapes the staging directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        limit = int(self.settings.upload_max_bytes)
        size = 0
        try:
            with target.open("wb") as handle:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > limit:
                        raise ValueError(
                            f"{clean_name} exceeds the {human_bytes(limit)} upload limit"
                        )
                    await asyncio.to_thread(handle.write, chunk)
        except BaseException:
            # A disconnected client or an over-limit body must not leave a
            # truncated file behind in the staging directory.
            target.unlink(missing_ok=True)
            raise
        return {
            "name": clean_name,
            "rel": rel_path,
            "size": size,
            "human_size": human_bytes(size),
            "batch": batch,
            "local": str(target),
        }

    async def push_batch(self, batch: str, dest: str = "") -> dict[str, Any]:
        """Copy a staged batch into the Kali container (one ``docker cp``)."""
        local = self.local_batch_dir(batch)
        files = [item for item in local.rglob("*") if item.is_file()]
        if not files:
            raise FileNotFoundError(f"no staged files for batch {batch}")
        total = sum(item.stat().st_size for item in files)
        clean_batch = safe_segment(batch, limit=64)
        target = self.resolve_container_path(
            dest
            or f"{self.container_root}/{safe_segment(self.settings.upload_dir_name)}/{clean_batch}"
        )
        parent = posixpath.dirname(target) or self.container_root
        incoming = f"{parent}/.{clean_batch}.incoming"
        prepared = await self.sandbox.exec(
            f"mkdir -p {shlex.quote(parent)} && rm -rf {shlex.quote(incoming)}", timeout=90
        )
        if not prepared.ok:
            raise RuntimeError(
                prepared.stderr or prepared.stdout or "could not prepare the container directory"
            )
        copied = await self.sandbox.copy_in(str(local), incoming)
        if not copied.ok:
            raise RuntimeError(copied.stderr or copied.stdout or "docker cp upload failed")
        moved = await self.sandbox.exec(
            " && ".join(
                (
                    f"rm -rf {shlex.quote(target)}",
                    f"mv {shlex.quote(incoming)} {shlex.quote(target)}",
                    f"chmod -R a+rwX {shlex.quote(target)}",
                )
            ),
            timeout=240,
        )
        if not moved.ok:
            raise RuntimeError(moved.stderr or moved.stdout or "could not publish the upload")
        return {
            "ok": True,
            "batch": clean_batch,
            "dest": target,
            "files": len(files),
            "bytes": total,
            "human_bytes": human_bytes(total),
            "paths": [
                f"{target}/{item.relative_to(local).as_posix()}" for item in files[:400]
            ],
        }

    # -- deletion ---------------------------------------------------------
    async def delete(self, path: str) -> dict[str, Any]:
        """Remove one file or one folder (recursively) from the workspace.

        The workspace root itself is never removed here - wiping everything has
        the dedicated :meth:`clear` entry point so a single click cannot be
        mistaken for a single-file delete.
        """
        resolved = self.resolve_container_path(path)
        if resolved.rstrip("/") == self.container_root.rstrip("/"):
            raise ValueError("use the workspace wipe action to delete the whole workspace")
        probe = await self.sandbox.exec(
            "if [ -d {0} ]; then echo dir; elif [ -f {0} ]; then echo file; else echo missing; fi".format(
                shlex.quote(resolved)
            ),
            timeout=60,
        )
        kind = next((line.strip() for line in reversed(probe.stdout.splitlines()) if line.strip()), "")
        if kind == "missing":
            raise FileNotFoundError(f"no such path in the sandbox: {resolved}")
        removed = await self.sandbox.exec(
            f"rm -rf -- {shlex.quote(resolved)}", timeout=300
        )
        if not removed.ok:
            raise RuntimeError(removed.stderr or removed.stdout or "could not delete the path")
        return {"ok": True, "path": resolved, "kind": kind, "deleted": True}

    async def clear(self) -> dict[str, Any]:
        """Wipe every file and folder inside the Kali workdir.

        Agent shells keep a ``cwd`` inside the tree that was just deleted, so the
        persistent terminals are closed as well; the next command respawns them
        at the workspace root.  That is what makes "delete workspace" leave a
        genuinely clean sandbox instead of six broken shells.
        """
        root = self.container_root
        # Keep the workspace itself (it is the mount point) and delete children.
        wiped = await self.sandbox.exec(
            f"find {shlex.quote(root)} -mindepth 1 -maxdepth 1 -exec rm -rf -- {{}} +",
            timeout=600,
        )
        await self.sandbox.close()
        if not wiped.ok:
            raise RuntimeError(wiped.stderr or wiped.stdout or "could not wipe the workspace")
        remaining = await self.sandbox.exec(
            f"ls -A {shlex.quote(root)} | wc -l", timeout=60
        )
        left = 0
        try:
            left = int((remaining.stdout or "0").strip().splitlines()[-1])
        except (ValueError, IndexError):
            left = 0
        return {"ok": True, "root": root, "remaining": left, "wiped": True}


    # -- listing / download ----------------------------------------------
    async def listing(self, path: str = "", limit: int | None = None) -> dict[str, Any]:
        root_path = self.resolve_container_path(path or self.container_root)
        cap = max(10, min(int(limit or self.settings.workspace_list_limit), 2_000))
        command = (
            f"find {shlex.quote(root_path)} -maxdepth 5 "
            r"\( -name .browser -o -name node_modules -o -name .git -o -name __pycache__ -o -name .cache \) -prune -o "
            "-printf '%y\\t%s\\t%T@\\t%p\\n' 2>/dev/null "
            "| sort -t $'\\t' -k3,3nr | head -n " + str(cap + 1)
        )
        result = await self.sandbox.exec(command, timeout=120)
        if not result.ok and not result.stdout:
            raise RuntimeError(result.stderr or "could not list the sandbox workspace")
        entries: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 3)
            if len(parts) != 4:
                continue
            kind, size, mtime, full = parts
            full = full.rstrip("/")
            if full == root_path:
                continue
            rel = full[len(root_path):].lstrip("/") if full.startswith(root_path) else full
            name = posixpath.basename(full) or root_path
            try:
                size_value = int(size)
            except ValueError:
                size_value = 0
            try:
                mtime_value = float(mtime)
            except ValueError:
                mtime_value = 0.0
            entries.append(
                {
                    "path": rel or ".",
                    "name": name,
                    "dir": kind == "d",
                    "size": size_value,
                    "human_size": "" if kind == "d" else human_bytes(size_value),
                    "mtime": mtime_value,
                }
            )
        truncated = len(entries) > cap
        entries = entries[:cap]
        # ``find`` output was sorted by mtime (newest first), so uploads and
        # freshly written agent artefacts surface at the top of the panel.
        return {
            "root": root_path,
            "count": len(entries),
            "truncated": truncated,
            "entries": entries,
        }

    async def download(self, path: str) -> tuple[Path, str, str, Path]:
        """Pull one file, or zip a directory, into a host temp directory.

        Returns ``(local_path, download_name, media_type, temp_dir)``; the
        caller owns ``temp_dir`` and must remove it after the response.
        """
        resolved = self.resolve_container_path(path)
        probe = await self.sandbox.exec(
            "if [ -d {0} ]; then echo dir; elif [ -f {0} ]; then echo file; else echo missing; fi".format(
                shlex.quote(resolved)
            ),
            timeout=60,
        )
        kind = next((line.strip() for line in reversed(probe.stdout.splitlines()) if line.strip()), "")
        workspace = Path(
            tempfile.mkdtemp(prefix="av2-download-", dir=str(self.settings.storage_dir))
        )
        if kind not in {"file", "dir"}:
            shutil.rmtree(workspace, ignore_errors=True)
            raise FileNotFoundError(f"no such path in the sandbox: {resolved}")
        if kind == "file":
            name = safe_segment(posixpath.basename(resolved) or "download", limit=160)
            local = workspace / name
            copied = await self.sandbox.copy_out(resolved, str(local))
            if not copied.ok or not local.exists():
                shutil.rmtree(workspace, ignore_errors=True)
                raise RuntimeError(copied.stderr or copied.stdout or "docker cp download failed")
            media = mimetypes.guess_type(name)[0] or "application/octet-stream"
            return local, name, media, workspace

        stem = safe_segment(posixpath.basename(resolved) or "work", limit=40)
        archive = f"av2-{stem}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        container_zip = f"/tmp/{archive}.zip"
        zipped = await self.sandbox.exec(
            "cd {0} && rm -f {1} && zip -qr {1} . -x '.browser/*' 'node_modules/*' '.git/*' '__pycache__/*'".format(
                shlex.quote(resolved), shlex.quote(container_zip)
            ),
            timeout=1_200,
        )
        suffix, media = ".zip", "application/zip"
        if not zipped.ok:
            container_zip = f"/tmp/{archive}.tar.gz"
            zipped = await self.sandbox.exec(
                "cd {0} && tar -czf {1} . --exclude=.browser --exclude=node_modules "
                "--exclude=.git --exclude=__pycache__".format(
                    shlex.quote(resolved), shlex.quote(container_zip)
                ),
                timeout=1_200,
            )
            suffix, media = ".tar.gz", "application/gzip"
        if not zipped.ok:
            shutil.rmtree(workspace, ignore_errors=True)
            raise RuntimeError(zipped.stderr or zipped.stdout or "could not build the archive")
        local = workspace / f"{archive}{suffix}"
        copied = await self.sandbox.copy_out(container_zip, str(local))
        await self.sandbox.exec(f"rm -f {shlex.quote(container_zip)}", timeout=30)
        if not copied.ok or not local.exists():
            shutil.rmtree(workspace, ignore_errors=True)
            raise RuntimeError(copied.stderr or copied.stdout or "docker cp archive failed")
        return local, f"{archive}{suffix}", media, workspace

    def discard(self, workspace: Path) -> None:
        """Remove the temp directory created by :meth:`download`."""
        shutil.rmtree(workspace, ignore_errors=True)


__all__ = ["FileExchange", "human_bytes", "safe_relative", "safe_segment"]

