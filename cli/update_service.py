"""Shared source-update helpers for the CLI and the Web console.

`cow update` and the console one-click updater must not drift: git pull,
dependency install, and the restart contract live here so both callers run
the same steps. GitHub is contacted only from `check_for_updates()`, never
from version/status reads or page load.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from cli.utils import get_project_root

GITHUB_REPO = "zhayujie/CowAgent"
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
STATUS_RELATIVE = Path("tmp") / "web-update-status.json"
BACKUP_DIRNAME = "backups"

ProgressCb = Callable[[str, str], None]


class UpdateError(Exception):
    """A single update step failed; `step` is the id that should be reported."""

    def __init__(self, step: str, message: str, output: str = ""):
        super().__init__(message)
        self.step = step
        self.message = message
        self.output = output or message


@dataclass
class InstallKind:
    kind: str
    update_supported: bool
    unsupported_reason: Optional[str] = None
    platform: str = field(default_factory=lambda: sys.platform)


def parse_version(value: str) -> tuple:
    """Parse a dotted version, ignoring a leading ``v`` and any suffix."""
    text = (value or "").strip()
    if text.lower().startswith("v") and text[1:2].isdigit():
        text = text[1:]
    parts = []
    for raw in text.split("."):
        digits = ""
        for char in raw:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits or 0))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def compare_versions(left: str, right: str) -> int:
    """Return 1 if left > right, -1 if left < right, else 0."""
    a, b = parse_version(left), parse_version(right)
    return (a > b) - (a < b)


def _is_docker_install() -> bool:
    if os.environ.get("COW_DOCKER") == "1":
        return True
    try:
        return Path("/.dockerenv").exists()
    except OSError:
        return False


def detect_install_kind(root: Optional[str] = None) -> InstallKind:
    """Classify this install so the UI can hide a broken Update button."""
    root = root or get_project_root()
    platform = sys.platform

    if os.environ.get("COW_DESKTOP") == "1" or getattr(sys, "frozen", False):
        return InstallKind(
            kind="packaged",
            update_supported=False,
            unsupported_reason=(
                "This desktop build is updated from the app's own updater, "
                "not with git pull."
            ),
            platform=platform,
        )
    if _is_docker_install():
        return InstallKind(
            kind="docker",
            update_supported=False,
            unsupported_reason=(
                "Docker installs are updated with `docker compose pull` "
                "and `docker compose up -d`."
            ),
            platform=platform,
        )
    if os.path.isdir(os.path.join(root, ".git")):
        if platform == "win32":
            return InstallKind(
                kind="git",
                update_supported=False,
                unsupported_reason=(
                    "Web one-click update cannot restart the Windows process "
                    "from inside itself. Run `cow update` in a terminal instead."
                ),
                platform=platform,
            )
        return InstallKind(kind="git", update_supported=True, platform=platform)
    return InstallKind(
        kind="unknown",
        update_supported=False,
        unsupported_reason=(
            "This install is not a git checkout, so the console cannot "
            "update it with `git pull`."
        ),
        platform=platform,
    )


def version_payload(root: Optional[str] = None) -> Dict[str, Any]:
    """Local version metadata. Must not contact GitHub."""
    from cli import __version__

    kind = detect_install_kind(root)
    return {
        "version": __version__,
        "install_kind": kind.kind,
        "update_supported": kind.update_supported,
        "unsupported_reason": kind.unsupported_reason,
        "platform": kind.platform,
    }


def _github_headers() -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github.v3+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_github_releases(timeout: int = 20) -> List[Dict[str, Any]]:
    import requests

    response = requests.get(
        GITHUB_RELEASES_URL,
        params={"per_page": 20},
        timeout=timeout,
        headers=_github_headers(),
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise UpdateError("check", "GitHub releases response was not a list")
    return data


def _release_brief(item: Dict[str, Any]) -> Dict[str, Any]:
    tag = str(item.get("tag_name") or item.get("name") or "").strip()
    return {
        "tag": tag,
        "name": item.get("name") or tag,
        "body": item.get("body") or "",
        "html_url": item.get("html_url") or "",
        "published_at": item.get("published_at") or "",
        "prerelease": bool(item.get("prerelease")),
        "draft": bool(item.get("draft")),
    }


def check_for_updates(current_version: Optional[str] = None) -> Dict[str, Any]:
    """Hit GitHub releases. Call only from an explicit user action."""
    from cli import __version__

    current = current_version or __version__
    releases = []
    for item in fetch_github_releases():
        if item.get("draft"):
            continue
        brief = _release_brief(item)
        if not brief["tag"]:
            continue
        releases.append(brief)

    newer = [
        item
        for item in releases
        if not item["prerelease"] and compare_versions(item["tag"], current) > 0
    ]
    newer.sort(key=lambda item: parse_version(item["tag"]), reverse=True)
    current_notes = next(
        (item for item in releases if compare_versions(item["tag"], current) == 0),
        None,
    )
    latest = newer[0] if newer else (releases[0] if releases else None)
    return {
        "status": "success",
        "current_version": current,
        "up_to_date": not newer,
        "latest": latest,
        "newer_releases": newer,
        "current_release": current_notes,
    }


def _run(cmd: List[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def git_rev_parse(root: str) -> str:
    proc = _run(["git", "rev-parse", "HEAD"], cwd=root)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def run_git_pull(root: str) -> subprocess.CompletedProcess:
    return _run(["git", "pull"], cwd=root)


def git_reset_hard(root: str, sha: str) -> subprocess.CompletedProcess:
    return _run(["git", "reset", "--hard", sha], cwd=root)


def install_requirements(root: str, python: str, quiet: bool = False) -> subprocess.CompletedProcess:
    req = os.path.join(root, "requirements.txt")
    extra = ["-q"] if quiet else []
    if not os.path.exists(req):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="")
    return _run([python, "-m", "pip", "install", "-r", req, *extra], cwd=root)


def install_editable(root: str, python: str, quiet: bool = False) -> subprocess.CompletedProcess:
    extra = ["-q"] if quiet else []
    return _run([python, "-m", "pip", "install", "-e", ".", *extra], cwd=root)


def install_python_dependencies(root: str, python: str, quiet: bool = False) -> subprocess.CompletedProcess:
    req = install_requirements(root, python, quiet=quiet)
    if req.returncode != 0:
        return req
    return install_editable(root, python, quiet=quiet)


def self_check_app(root: str, python: str) -> subprocess.CompletedProcess:
    return _run([python, "-c", "import app"], cwd=root)


def create_pre_update_backup(root: Optional[str] = None) -> Dict[str, Any]:
    from cli.commands.backup import (
        _data_root,
        _read_config,
        _workspace_from_config,
        create_backup_archive,
    )

    root_path = Path(root or get_project_root()).resolve()
    data_root = _data_root()
    workspace = _workspace_from_config(_read_config(data_root))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = root_path / BACKUP_DIRNAME
    backup_dir.mkdir(parents=True, exist_ok=True)
    output = backup_dir / f"cow-pre-update-{stamp}.zip"
    return create_backup_archive(
        output,
        data_root,
        workspace,
        excluded_paths=[backup_dir],
    )


def status_path(root: Optional[str] = None) -> Path:
    path = Path(root or get_project_root()) / STATUS_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read_update_status(root: Optional[str] = None) -> Dict[str, Any]:
    path = status_path(root)
    if not path.is_file():
        return {"state": "idle"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "idle"}
    return data if isinstance(data, dict) else {"state": "idle"}


def write_update_status(payload: Dict[str, Any], root: Optional[str] = None) -> Dict[str, Any]:
    path = status_path(root)
    current = read_update_status(root)
    current.update(payload)
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return current


def _emit(progress: Optional[ProgressCb], step: str, message: str) -> None:
    if progress:
        progress(step, message)


def apply_source_update(
    root: str,
    *,
    python: Optional[str] = None,
    quiet: bool = False,
    progress: Optional[ProgressCb] = None,
    restore_sha: Optional[str] = None,
) -> None:
    """Run git pull + pip. Raises UpdateError; resets to restore_sha on failure after pull."""
    python = python or sys.executable
    previous = restore_sha or git_rev_parse(root)

    _emit(progress, "git_pull", "Pulling latest code")
    pull = run_git_pull(root)
    if pull.returncode != 0:
        raise UpdateError("git_pull", "git pull failed", pull.stdout or "")

    try:
        _emit(progress, "install_deps", "Installing Python dependencies")
        deps = install_requirements(root, python, quiet=quiet)
        if deps.returncode != 0:
            raise UpdateError(
                "install_deps",
                "Dependency install failed",
                deps.stdout or "",
            )
        _emit(progress, "install_cli", "Reinstalling the cow CLI")
        cli = install_editable(root, python, quiet=quiet)
        if cli.returncode != 0:
            raise UpdateError(
                "install_cli",
                "CLI reinstall failed",
                cli.stdout or "",
            )
        _emit(progress, "self_check", "Checking that the new code loads")
        check = self_check_app(root, python)
        if check.returncode != 0:
            raise UpdateError(
                "self_check",
                "New code failed to import; leaving the running service untouched",
                check.stdout or "",
            )
    except UpdateError:
        if previous:
            git_reset_hard(root, previous)
        raise


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _restart_unix_service(root: str, python: str, old_pid: int, log_file: str, pid_file: str) -> int:
    import signal

    if _pid_alive(old_pid):
        try:
            os.kill(old_pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(150):
            if not _pid_alive(old_pid):
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(old_pid, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(0.5)

    app_py = os.path.join(root, "app.py")
    with open(log_file, "a", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [python, app_py],
            cwd=root,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
    Path(pid_file).write_text(str(proc.pid), encoding="utf-8")
    return proc.pid


def run_web_update_worker() -> None:
    """Detached entry point: backup, update, self-check, then restart."""
    root = get_project_root()
    status = read_update_status(root)
    python = status.get("python") or sys.executable
    old_pid = int(status.get("old_pid") or 0)
    log_file = status.get("log_file") or os.path.join(root, "nohup.out")
    pid_file = status.get("pid_file") or os.path.join(root, ".cow.pid")
    previous_sha = git_rev_parse(root)

    def progress(step: str, message: str) -> None:
        write_update_status(
            {
                "state": "running",
                "step": step,
                "message": message,
                "previous_sha": previous_sha,
            },
            root,
        )

    try:
        write_update_status(
            {
                "state": "running",
                "step": "backup",
                "message": "Creating a user-data backup",
                "previous_sha": previous_sha,
                "error": None,
                "output": "",
            },
            root,
        )
        backup = create_pre_update_backup(root)
        archive = backup.get("archive") or ""
        write_update_status({"backup_path": archive}, root)

        apply_source_update(
            root,
            python=python,
            quiet=False,
            progress=progress,
            restore_sha=previous_sha,
        )

        write_update_status(
            {
                "state": "restarting",
                "step": "restart",
                "message": "Restarting the service",
            },
            root,
        )
        new_pid = _restart_unix_service(root, python, old_pid, log_file, pid_file)
        write_update_status(
            {
                "state": "success",
                "step": "done",
                "message": "Update complete",
                "new_pid": new_pid,
            },
            root,
        )
    except UpdateError as exc:
        write_update_status(
            {
                "state": "failed",
                "step": exc.step,
                "message": exc.message,
                "error": exc.message,
                "output": exc.output,
                "previous_sha": previous_sha,
            },
            root,
        )
    except Exception as exc:
        write_update_status(
            {
                "state": "failed",
                "step": "unknown",
                "message": str(exc),
                "error": str(exc),
                "output": str(exc),
                "previous_sha": previous_sha,
            },
            root,
        )


def schedule_web_update(root: Optional[str] = None) -> Dict[str, Any]:
    """Spawn the detached updater. Never kills this process from the request."""
    if sys.platform == "win32":
        kind = detect_install_kind(root)
        raise UpdateError(
            "restart",
            kind.unsupported_reason or "Web update is not supported on Windows",
        )

    root = root or get_project_root()
    kind = detect_install_kind(root)
    if not kind.update_supported:
        raise UpdateError("start", kind.unsupported_reason or "Update is not supported")

    current = read_update_status(root)
    if current.get("state") in {"running", "restarting"}:
        raise UpdateError("start", "An update is already in progress")

    from cli.commands.process import _get_log_file, _get_pid_file, _read_pid

    old_pid = _read_pid() or os.getpid()
    payload = write_update_status(
        {
            "state": "running",
            "step": "starting",
            "message": "Starting update",
            "python": sys.executable,
            "old_pid": old_pid,
            "log_file": _get_log_file(),
            "pid_file": _get_pid_file(),
            "error": None,
            "output": "",
            "backup_path": "",
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
        root,
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.Popen(
        [sys.executable, "-m", "cli.update_service"],
        cwd=root,
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return payload


if __name__ == "__main__":
    run_web_update_worker()
