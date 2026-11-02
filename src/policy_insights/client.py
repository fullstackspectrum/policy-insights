"""Fetch policy decision logs from the Cloudsmith v2 API."""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx

from . import __version__
from .analysis import DayAgg
from .auth import Credential

try:
    import orjson

    _loads = orjson.loads
except ImportError:  # pragma: no cover
    import json

    _loads = json.loads

API_BASE = os.environ.get("CLOUDSMITH_API_HOST", "https://api.cloudsmith.io").rstrip("/") + "/v2"

# Don't split a job whose remaining time range is shorter than this.
MIN_SPLIT = timedelta(minutes=2)
_ULID_ALPHABET = {c: i for i, c in enumerate("0123456789ABCDEFGHJKMNPQRSTVWXYZ")}


class ApiError(RuntimeError):
    pass


class _Cancelled(Exception):
    pass


@dataclass
class FetchStats:
    logs: int = 0
    requests: int = 0
    request_seconds: float = 0.0
    throttled: int = 0
    paused_seconds: float = 0.0
    splits: int = 0
    days_done: int = 0
    days_total: int = 0

    @property
    def avg_latency(self) -> float:
        return self.request_seconds / self.requests if self.requests else 0.0


@dataclass
class _Job:
    day: date
    start: datetime
    end: datetime
    after_id: str | None = None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ulid_time(ulid: str) -> datetime | None:
    try:
        ms = 0
        for c in ulid[:10]:
            ms = ms * 32 + _ULID_ALPHABET[c]
        return datetime.fromtimestamp(ms / 1000, timezone.utc)
    except (KeyError, ValueError, OverflowError):
        return None


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"
    detail = body.get("detail", "") if isinstance(body, dict) else body
    fields = body.get("fields") if isinstance(body, dict) else None
    msg = f"HTTP {resp.status_code}: {detail}"
    if fields:
        msg += " " + "; ".join(f"{k}: {', '.join(map(str, v))}" for k, v in fields.items())
    return msg


class _RateGate:
    """Shared back-off so one throttled worker pauses all of them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until = 0.0

    def pause(self, seconds: float) -> None:
        with self._lock:
            self._until = max(self._until, time.monotonic() + seconds)

    def wait(self, stop: threading.Event) -> float:
        waited = 0.0
        while not stop.is_set():
            with self._lock:
                delay = self._until - time.monotonic()
            if delay <= 0:
                break
            step = min(delay, 1.0)
            stop.wait(step)
            waited += step
        return waited


class _Fetcher:
    """A pool of workers pulling time-range jobs from a shared queue.

    Every job starts as one UTC day. While a worker is paging through a job
    and another worker is idle, it splits off the back half of its remaining
    time range as a new job, so a single dense day still fans out across all
    workers. The split job reuses the current `after_id` cursor and takes the
    later `created_after`, so the two jobs partition the remaining logs
    exactly with no overlap.
    """

    def __init__(self, client: httpx.Client, url: str, base_params: dict, workers: int,
                 on_day: Callable[[DayAgg], None], on_progress: Callable[[FetchStats], None] | None) -> None:
        self.client = client
        self.url = url
        self.base_params = base_params
        self.workers = workers
        self.on_day = on_day
        self.on_progress = on_progress
        self.gate = _RateGate()
        self.stop = threading.Event()
        self.jobs: queue.SimpleQueue[_Job | None] = queue.SimpleQueue()
        self.stats = FetchStats()
        self._lock = threading.Lock()
        self._busy = 0
        self._outstanding_jobs = 0
        self._day_jobs: dict[date, int] = {}
        self._day_aggs: dict[date, DayAgg] = {}
        self._error: BaseException | None = None
        # The server may cap page_size below what we asked for, so a page shorter
        # than requested isn't proof the job is exhausted. A page shorter than
        # the longest page seen so far is, since the cap must be at least that.
        self._longest_page = 0

    def run(self, days: list[date]) -> FetchStats:
        self.stats.days_total = len(days)
        for day in days:
            start = datetime.combine(day, datetime.min.time(), timezone.utc)
            end = min(start + timedelta(days=1), datetime.now(timezone.utc))
            self._submit(_Job(day, start, end))
        threads = [threading.Thread(target=self._worker, name=f"cs-fetch-{i}", daemon=True) for i in range(self.workers)]
        for t in threads:
            t.start()
        try:
            for t in threads:
                while t.is_alive():
                    t.join(0.2)
        except BaseException:
            self.stop.set()
            raise
        if self._error:
            raise self._error
        return self.stats

    def _submit(self, job: _Job) -> None:
        with self._lock:
            self._outstanding_jobs += 1
            self._day_jobs[job.day] = self._day_jobs.get(job.day, 0) + 1
        self.jobs.put(job)

    def _worker(self) -> None:
        while not self.stop.is_set():
            try:
                job = self.jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is None:
                return
            with self._lock:
                self._busy += 1
            try:
                agg = self._run_job(job)
                self._finish_job(job, agg)
            except _Cancelled:
                return
            except BaseException as exc:  # surface the first error and stop everyone
                with self._lock:
                    self._error = self._error or exc
                self.stop.set()
                return
            finally:
                with self._lock:
                    self._busy -= 1

    def _run_job(self, job: _Job) -> DayAgg:
        agg = DayAgg(job.day)
        params = dict(self.base_params)
        while True:
            params["created_after"] = _iso(job.start)
            params["created_before"] = _iso(job.end)
            if job.after_id:
                params["after_id"] = job.after_id
            results = _loads(self._get(params).content).get("results", [])
            if not results:
                break
            agg.add(results)
            with self._lock:
                self.stats.logs += len(results)
                self._longest_page = max(self._longest_page, len(results))
                last_page = len(results) < self._longest_page
                idle = self._busy < self.workers and self.jobs.empty()
            self._progress()
            next_id = results[-1]["id"]
            if last_page or next_id == job.after_id:
                break
            job.after_id = next_id
            if idle:
                self._maybe_split(job)
        return agg

    def _maybe_split(self, job: _Job) -> None:
        cursor = _ulid_time(job.after_id) if job.after_id else None
        if cursor is None or not (job.start <= cursor < job.end):
            cursor = job.start
        if job.end - cursor < MIN_SPLIT:
            return
        mid = cursor + (job.end - cursor) / 2
        mid = mid.replace(microsecond=mid.microsecond // 1000 * 1000)  # API resolves to the millisecond
        self._submit(_Job(job.day, mid, job.end, job.after_id))
        job.end = mid
        with self._lock:
            self.stats.splits += 1

    def _finish_job(self, job: _Job, agg: DayAgg) -> None:
        done_day = None
        with self._lock:
            day_agg = self._day_aggs.get(job.day)
            if day_agg is None:
                self._day_aggs[job.day] = agg
            else:
                day_agg.merge(agg)
            self._day_jobs[job.day] -= 1
            if self._day_jobs[job.day] == 0:
                done_day = self._day_aggs.pop(job.day)
                self.stats.days_done += 1
            self._outstanding_jobs -= 1
            all_done = self._outstanding_jobs == 0
        if done_day is not None:
            self.on_day(done_day)
            self._progress()
        if all_done:
            for _ in range(self.workers):
                self.jobs.put(None)

    def _progress(self) -> None:
        if self.on_progress:
            self.on_progress(self.stats)

    def _get(self, params: dict, retries: int = 6) -> httpx.Response:
        resp = None
        for attempt in range(retries):
            paused = self.gate.wait(self.stop)
            if self.stop.is_set():
                raise _Cancelled()
            started = time.perf_counter()
            try:
                resp = self.client.get(self.url, params=params)
            except httpx.TransportError as exc:
                if attempt == retries - 1:
                    raise ApiError(f"Network error talking to Cloudsmith: {exc}") from exc
                time.sleep(min(2**attempt, 30))
                continue
            finally:
                with self._lock:
                    self.stats.requests += 1
                    self.stats.request_seconds += time.perf_counter() - started
                    self.stats.paused_seconds += paused
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                with self._lock:
                    self.stats.throttled += resp.status_code == 429
                self.gate.pause(float(retry_after) if retry_after else min(2**attempt, 30))
                continue
            if resp.headers.get("X-RateLimit-Remaining") == "0" and (reset := resp.headers.get("X-RateLimit-Reset")):
                try:
                    self.gate.pause(max(0.0, float(reset) - time.time()))
                except ValueError:
                    pass
            if resp.status_code == 404:
                raise ApiError(f"Workspace not found (or decision logs unavailable): {_error_message(resp)}")
            if resp.status_code in (401, 403):
                raise ApiError(f"Not authorised: {_error_message(resp)}")
            if resp.status_code >= 400:
                raise ApiError(_error_message(resp))
            return resp
        raise ApiError(_error_message(resp) if resp is not None else "Request failed")


def fetch_days(
    credential: Credential,
    workspace: str,
    days: list[date],
    on_day: Callable[[DayAgg], None],
    *,
    policy: str | None = None,
    repository: str | None = None,
    package_formats: list[str] | None = None,
    package_name: str | None = None,
    page_size: int = 1000,
    workers: int = 8,
    on_progress: Callable[[FetchStats], None] | None = None,
) -> FetchStats:
    """Fetch every decision log for the given UTC days, calling `on_day` with
    each day's aggregates as soon as that day is complete."""
    params: dict[str, object] = {"sort": "id", "page_size": page_size}
    if policy:
        params["policy_slug_perm"] = policy
    if repository:
        params["repository_slug_perm"] = repository
    if package_formats:
        params["package_format"] = [f.upper() for f in package_formats]
    if package_name:
        params["package_name"] = package_name

    headers = {
        **credential.headers,
        "Accept": "application/json",
        "User-Agent": f"policy-insights/{__version__}",
    }
    url = f"{API_BASE}/workspaces/{workspace}/policies/decision-logs-v1/"
    limits = httpx.Limits(max_connections=workers, max_keepalive_connections=workers)
    with httpx.Client(headers=headers, timeout=60, limits=limits) as client:
        return _Fetcher(client, url, params, workers, on_day, on_progress).run(days)
