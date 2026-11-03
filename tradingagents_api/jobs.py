"""Run bookkeeping on disk, and the pool that runs them.

Each run is a folder under ``runs/``: ``job.json`` (what to run), ``status.json``
(written only by the server), ``result.json`` (written by the worker process),
``worker.log``, ``report/`` and ``report.zip``. Keeping it all on a volume means a
restart loses nothing: queued runs are queued again, interrupted ones are failed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from tradingagents_api.settings import Settings

log = logging.getLogger("tradingagents_api")

RUN_ID = re.compile(r"^[0-9a-f]{32}$")
ACTIVE = ("queued", "running")
FINISHED = ("succeeded", "failed", "cancelled")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class JobManager:
    # The worker process; tests swap in a stand-in.
    worker_command = (sys.executable, "-m", "tradingagents_api.worker")

    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.runs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}
        self._pool = ThreadPoolExecutor(max_workers=settings.max_concurrent_runs,
                                        thread_name_prefix="run")
        self._stop = threading.Event()
        self._janitor = threading.Thread(target=self._janitor_loop, daemon=True, name="janitor")

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._recover()
        self._janitor.start()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            for proc in self._procs.values():
                proc.terminate()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _recover(self) -> None:
        """After a restart: re-queue what never started, fail what was cut off."""
        for status in sorted(self._all_statuses(), key=lambda s: s["created_at"]):
            if status["status"] == "running":
                self._update(status["id"], status="failed", finished_at=_now(),
                             error="the server restarted while the run was in progress")
            elif status["status"] == "queued":
                self._pool.submit(self._execute, status["id"])

    # -- paths and status ----------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        if not RUN_ID.match(run_id):
            raise KeyError(run_id)
        return self.settings.runs_dir / run_id

    def get(self, run_id: str) -> dict | None:
        try:
            return _read_json(self.run_dir(run_id) / "status.json")
        except KeyError:
            return None

    def _all_statuses(self) -> list[dict]:
        out = []
        for path in self.settings.runs_dir.glob("*/status.json"):
            status = _read_json(path)
            if status:
                out.append(status)
        return out

    def list(self, limit: int = 50) -> list[dict]:
        return sorted(self._all_statuses(), key=lambda s: s["created_at"], reverse=True)[:limit]

    def _update(self, run_id: str, **fields) -> dict:
        with self._lock:
            path = self.run_dir(run_id) / "status.json"
            status = _read_json(path) or {}
            status.update(fields)
            _write_json(path, status)
            return status

    # -- operations ----------------------------------------------------------

    def submit(self, job: dict, callback_url: str | None) -> dict:
        run_id = uuid.uuid4().hex
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True)
        _write_json(run_dir / "job.json", job)
        status = {
            "id": run_id,
            "status": "queued",
            "ticker": job["ticker"],
            "date": job["date"],
            "asset_type": job["asset_type"],
            "analysts": job["analysts"],
            "portfolio_source": job["portfolio_source"],
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "decision": None,
            "error": None,
            "callback_url": callback_url,
        }
        _write_json(run_dir / "status.json", status)
        self._pool.submit(self._execute, run_id)
        return status

    def delete(self, run_id: str) -> bool:
        """Cancel the run if it is active, then remove it and its files."""
        status = self.get(run_id)
        if status is None:
            return False
        if status["status"] in ACTIVE:
            self._update(run_id, status="cancelled", finished_at=_now(), error="cancelled")
            with self._lock:
                proc = self._procs.get(run_id)
            if proc is not None:
                proc.terminate()
        shutil.rmtree(self.run_dir(run_id), ignore_errors=True)
        return True

    def _execute(self, run_id: str) -> None:
        status = self.get(run_id)
        if status is None or status["status"] != "queued":
            return  # deleted or cancelled while waiting
        run_dir = self.run_dir(run_id)
        self._update(run_id, status="running", started_at=_now())
        error = None
        try:
            with open(run_dir / "worker.log", "wb") as logfile:
                proc = subprocess.Popen(
                    [*self.worker_command, str(run_dir)],
                    stdout=logfile, stderr=subprocess.STDOUT, cwd=str(run_dir),
                )
                with self._lock:
                    self._procs[run_id] = proc
                try:
                    proc.wait(timeout=self.settings.run_timeout_seconds)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    error = f"timed out after {self.settings.run_timeout_seconds}s"
        except OSError as exc:
            error = f"could not start the worker: {exc}"
        finally:
            with self._lock:
                self._procs.pop(run_id, None)

        current = self.get(run_id)
        if current is None or current["status"] == "cancelled":
            return
        result = _read_json(run_dir / "result.json") or {}
        if error is None and result.get("ok") and (run_dir / "report.zip").is_file():
            final = self._update(run_id, status="succeeded", finished_at=_now(),
                                 decision=result.get("decision"))
        else:
            final = self._update(run_id, status="failed", finished_at=_now(),
                                 error=error or result.get("error") or "the worker exited without a result; see worker.log")
        self._notify(final)

    # -- callbacks and cleanup ------------------------------------------------

    def _notify(self, status: dict) -> None:
        url = status.get("callback_url")
        if not url:
            return
        body = json.dumps(public_status(status)).encode()
        headers = {"Content-Type": "application/json", "X-TradingAgents-Run-Id": status["id"]}
        if self.settings.callback_secret:
            digest = hmac.new(self.settings.callback_secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-TradingAgents-Signature"] = f"sha256={digest}"
        for attempt in range(3):
            try:
                resp = requests.post(url, data=body, headers=headers, timeout=10)
                if resp.status_code < 500:
                    return
            except requests.RequestException as exc:
                log.warning("callback for %s failed: %s", status["id"], exc)
            time.sleep(2 ** attempt)

    def _janitor_loop(self) -> None:
        while not self._stop.wait(600):
            try:
                self.cleanup()
            except Exception:  # noqa: BLE001 - never let cleanup kill the thread
                log.exception("cleanup failed")

    def cleanup(self) -> int:
        """Remove finished runs older than the TTL; return how many."""
        cutoff = datetime.now(UTC) - timedelta(hours=self.settings.run_ttl_hours)
        removed = 0
        for status in self._all_statuses():
            finished = status.get("finished_at")
            if status["status"] in FINISHED and finished and datetime.fromisoformat(finished) < cutoff:
                shutil.rmtree(self.run_dir(status["id"]), ignore_errors=True)
                removed += 1
        return removed


def public_status(status: dict, base_url: str = "") -> dict:
    """The status as the API returns it."""
    out = {k: v for k, v in status.items() if k != "callback_url"}
    out["status_url"] = f"{base_url}/runs/{status['id']}"
    out["report_url"] = f"{base_url}/runs/{status['id']}/report" if status["status"] == "succeeded" else None
    return out
