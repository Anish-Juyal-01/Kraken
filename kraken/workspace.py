"""Kraken's own checkouts of connected repos (for detection and Sonar's code index)."""

import asyncio
import re
from pathlib import Path

CLONE_TIMEOUT_S = 600
_SAFE_URL = re.compile(r"^(https://[\w.-]+/[\w./~-]+|git@[\w.-]+:[\w./~-]+)$")


class GitError(RuntimeError):
    pass


def valid_repo_url(url: str) -> bool:
    """https:// or git@ URLs only: no local paths, no option-looking strings."""
    return bool(_SAFE_URL.match(url)) and not url.startswith("-")


def project_name(url: str) -> str:
    raw = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
    return re.sub(r"[^\w.-]", "-", raw) or "project"


async def _git(*args: str, cwd: Path | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), CLONE_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise GitError(f"git {args[0]} timed out after {CLONE_TIMEOUT_S}s") from None
    if proc.returncode:
        raise GitError(err.decode("utf-8", "replace").strip()[-400:] or f"git {args[0]} failed")
    return out.decode("utf-8", "replace")


async def checkout(url: str, dest: Path) -> Path:
    """Clone `url` into `dest`, or update it when it is already there."""
    if not valid_repo_url(url):
        raise GitError(f"not a repo URL I can clone: {url}")
    if (dest / ".git").is_dir():
        await _git("pull", "--ff-only", "--quiet", cwd=dest)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await _git("clone", "--depth", "50", "--quiet", "--", url, str(dest))
    return dest
