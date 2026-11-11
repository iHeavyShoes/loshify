"""Background jobs for the Desktop's long steps ("Get to know me", the portrait rewrite, "Build my playlists").

A Desktop REST call has a client-side timeout, and a build (host LLM + Spotify searches) can outlast it.
So the route starts a job and answers at once; the card polls ``status(job_id)`` and shows its progress
line. A client abort or timeout therefore never means failure: the job keeps running here and the card
just polls again.

* The **client** picks the job id and sends it with the start request. If the answer to that request is
  lost, the card still knows what to ask for, and sending the same id again is a no-op (idempotent).
* One job per *kind* and scope at a time: a second "Build" while one runs returns the running job.
* Jobs live in this process only. The caller persists the finished result (``on_finish``) so a status
  question after a restart still gets the answer.
* ``fn(progress)`` returns the route envelope (``{ok, data}`` / ``{ok: false, error}``); it never raises
  (the dashboard's ``_run`` maps every exception to a plain envelope). If it raises anyway, the job fails
  with a generic message, never with the exception text.
"""

from __future__ import annotations

import contextvars
import re
import threading
import time
from typing import Any, Callable, Dict, Optional

RUNNING, DONE, FAILED, UNKNOWN = "running", "done", "failed", "unknown"
STALE_S = 10 * 60          # a job still "running" after this is reported as failed (a hung provider call)
KEEP = 16                  # finished jobs kept in memory per registry
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

Progress = Callable[[str], None]


def valid_id(job_id: Any) -> bool:
    return isinstance(job_id, str) and bool(_ID_RE.match(job_id))


def succeeded(envelope: Any) -> bool:
    """The route envelope says the step worked (transport ok and the step's own ``ok`` is not False)."""
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        return False
    data = envelope.get("data")
    return not (isinstance(data, dict) and data.get("ok") is False)


def failure_message(envelope: Any) -> Optional[str]:
    """The step's own plain-language reason, if it gave one (``None`` = let the card say it kindly)."""
    if not isinstance(envelope, dict):
        return None
    data = envelope.get("data")
    msg = (data.get("error") if isinstance(data, dict) else None) or envelope.get("error")
    return msg if isinstance(msg, str) and msg.strip() else None


class Jobs:
    """Thread-backed job registry. ``clock`` is injectable for tests."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, dict] = {}
        self._clock = clock

    # -- views ------------------------------------------------------------------------------------

    def _view(self, job: dict) -> dict:
        out = {"id": job["id"], "kind": job["kind"], "state": job["state"], "progress": job["progress"],
               "started": job["started"], "finished": job["finished"]}
        if job["state"] == RUNNING and self._clock() - job["started"] > STALE_S:
            out.update(state=FAILED, message=None, stale=True)
            return out
        if job["state"] in (DONE, FAILED):
            out["result"] = job["result"]
            out["message"] = job["message"]
        return out

    def get(self, job_id: str) -> Optional[dict]:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._view(job) if job else None

    def running(self, kind: str, scope: str = "") -> Optional[dict]:
        with self._lock:
            for job in self._jobs.values():
                if job["kind"] == kind and job["scope"] == scope and job["state"] == RUNNING \
                        and (self._clock() - job["started"] <= STALE_S or
                             (job.get("thread") is not None and job["thread"].is_alive())):
                    return self._view(job)
        return None

    # -- start ------------------------------------------------------------------------------------

    def start(self, job_id: str, kind: str, fn: Callable[[Progress], Dict[str, Any]], *, scope: str = "",
              first: str = "", on_finish: Optional[Callable[[dict], None]] = None,
              run_inline: bool = False) -> dict:
        """Start *fn* as job *job_id* (or return the job already known under that id / running for *kind*).

        ``run_inline`` runs it in the calling thread (tests). The current ``contextvars`` context (the
        profile secret scope the server bound) is carried into the worker thread.
        """
        if not valid_id(job_id):
            raise ValueError("job must be 8-64 letters, digits, '-' or '_'")
        with self._lock:
            known = self._jobs.get(job_id)
            if known is not None:
                return self._view(known)
            for other in self._jobs.values():
                if other["kind"] == kind and other["scope"] == scope and other["state"] == RUNNING \
                        and (self._clock() - other["started"] <= STALE_S or
                             (other.get("thread") is not None and other["thread"].is_alive())):
                    return self._view(other)
            job = {"id": job_id, "kind": kind, "scope": scope, "state": RUNNING, "progress": first,
                   "started": self._clock(), "finished": None, "result": None, "message": None}
            self._jobs[job_id] = job
            self._prune()
            ctx = contextvars.copy_context()
            if not run_inline:
                thread = threading.Thread(target=ctx.run, args=(self._work, job, fn, on_finish),
                                          name=f"loshify-{kind}-{job_id[:8]}", daemon=True)
                job["thread"] = thread
                thread.start()
        if run_inline:
            ctx.run(self._work, job, fn, on_finish)
        return self.get(job_id) or {}

    def _work(self, job: dict, fn: Callable[[Progress], Dict[str, Any]],
              on_finish: Optional[Callable[[dict], None]]) -> None:
        def progress(text: str) -> None:
            with self._lock:
                job["progress"] = str(text or "")[:120]

        try:
            result = fn(progress)
        except Exception:  # fn should not raise; never pass exception text to the card
            result = {"ok": False, "error": None}
        ok = succeeded(result)
        with self._lock:
            job.update(state=DONE if ok else FAILED, result=result if ok else None,
                       message=None if ok else failure_message(result), finished=self._clock())
            view = self._view(job)
        if on_finish is not None:
            try:
                on_finish(view)
            except Exception:
                pass

    def _prune(self) -> None:
        finished = sorted((j for j in self._jobs.values() if j["state"] != RUNNING), key=lambda j: j["started"])
        for job in finished[:max(0, len(finished) - KEEP)]:
            self._jobs.pop(job["id"], None)


__all__ = ["DONE", "FAILED", "Jobs", "RUNNING", "STALE_S", "UNKNOWN", "failure_message", "succeeded", "valid_id"]
