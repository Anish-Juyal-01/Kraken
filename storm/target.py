"""Start, stop and health-check the app under test from its project `run` command."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx


def healthy(base_url: str, health: str | None, timeout: float = 2.0) -> bool:
    """True when the app answers its health path with any non-5xx status."""
    try:
        r = httpx.get(base_url.rstrip("/") + (health or "/"), timeout=timeout)
        return r.status_code < 500
    except httpx.HTTPError:
        return False


class Target:
    """The app, run by Storm in its own process group so a kill takes its children too."""

    def __init__(self, run: str, cwd: Path, base_url: str, health: str | None,
                 env: dict[str, str] | None = None, start_timeout: float = 60) -> None:
        self.run, self.cwd, self.base_url, self.health = run, cwd, base_url, health
        self.env = env
        self.start_timeout = start_timeout
        self.proc: subprocess.Popen | None = None
        self.log = cwd / ".storm-target.log"
        self._log_file = None

    def _close_log(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    @property
    def owned(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> float:
        """Start the app and wait until it is healthy; returns the seconds it took."""
        began = time.monotonic()
        self._close_log()
        self._log_file = self.log.open("ab")
        kwargs: dict = {"cwd": self.cwd, "shell": True, "env": self.env,
                        "stdout": self._log_file, "stderr": subprocess.STDOUT}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self.proc = subprocess.Popen(self.run, **kwargs)  # noqa: S602  (the project's own command)
        while time.monotonic() - began < self.start_timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"`{self.run}` exited with {self.proc.returncode}; see {self.log}")
            if healthy(self.base_url, self.health, timeout=1):
                return round(time.monotonic() - began, 2)
            time.sleep(0.3)
        self.stop()
        raise RuntimeError(f"`{self.run}` was not healthy at {self.base_url} within "
                           f"{self.start_timeout:g}s; see {self.log}")

    def stop(self) -> None:
        """Kill the app and everything it started."""
        if self.proc is None or self.proc.poll() is not None:
            self.proc = None
            self._close_log()
            return
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                           capture_output=True, check=False)
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        self.proc = None
        self._close_log()

    def __enter__(self) -> "Target":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
