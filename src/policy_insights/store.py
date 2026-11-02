"""SQLite store of per-day aggregates, used as a cache between runs.

Decision logs for a finished UTC day never change, so once a day is fetched
in full its aggregates are kept and later runs only fetch what's missing.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .analysis import Bucket, DayAgg, Summary, day_range, trend

SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    day TEXT PRIMARY KEY, complete INTEGER NOT NULL, logs INTEGER NOT NULL,
    evaluations INTEGER NOT NULL, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counts (
    day TEXT, policy TEXT, repo TEXT, format TEXT, matched INTEGER, n INTEGER
);
CREATE INDEX IF NOT EXISTS counts_day ON counts(day);
CREATE TABLE IF NOT EXISTS packages (day TEXT, format TEXT, name TEXT, policy TEXT, n INTEGER);
CREATE INDEX IF NOT EXISTS packages_day ON packages(day);
CREATE TABLE IF NOT EXISTS package_days (day TEXT, pkg TEXT, PRIMARY KEY (day, pkg)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS policies (
    slug TEXT PRIMARY KEY, name TEXT, version INTEGER, terminal INTEGER, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS repos (slug TEXT PRIMARY KEY, name TEXT);
"""

# A day is only cached as complete once it ended this long ago, to allow for
# logs that land a little after their creation timestamp.
SETTLE = timedelta(hours=1)


def cache_path(workspace: str, filters: dict) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    root = base / "policy-insights"
    legacy = base / "cs-decisions"  # the tool's former name
    if legacy.is_dir() and not root.exists():
        legacy.rename(root)
    key = json.dumps({k: sorted(v) if isinstance(v, list) else v for k, v in filters.items() if v}, sort_keys=True)
    digest = hashlib.sha256(key.encode()).hexdigest()[:10]
    return root / f"{workspace}-{digest}.sqlite"


def is_settled(day: date, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc)
    return end + SETTLE <= now


class Store:
    def __init__(self, path: Path | str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def complete_days(self) -> set[date]:
        rows = self.db.execute("SELECT day FROM days WHERE complete = 1")
        return {date.fromisoformat(d) for (d,) in rows}

    def save_day(self, agg: DayAgg, complete: bool) -> None:
        day = agg.day.isoformat()
        with self._lock, self.db:
            for table in ("days", "counts", "packages", "package_days"):
                self.db.execute(f"DELETE FROM {table} WHERE day = ?", (day,))
            self.db.execute(
                "INSERT INTO days VALUES (?, ?, ?, ?, ?)",
                (day, int(complete), agg.logs, len(agg.correlations - {None}),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            self.db.executemany(
                "INSERT INTO counts VALUES (?, ?, ?, ?, ?, ?)",
                ((day, p, r, f, int(m), n) for (p, r, f, m), n in agg.counts.items()),
            )
            self.db.executemany(
                "INSERT INTO packages VALUES (?, ?, ?, ?, ?)",
                ((day, f, name, p, n) for (f, name, p), n in agg.packages.items()),
            )
            self.db.executemany(
                "INSERT INTO package_days VALUES (?, ?)", ((day, pkg) for pkg in agg.package_ids if pkg)
            )
            self.db.executemany(
                """INSERT INTO policies VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET name = excluded.name, version = excluded.version,
                   terminal = excluded.terminal, last_seen = excluded.last_seen
                   WHERE excluded.last_seen >= policies.last_seen""",
                ((slug, name, version, int(terminal), seen) for slug, (name, version, terminal, seen) in agg.policies.items()),
            )
            self.db.executemany(
                "INSERT INTO repos VALUES (?, ?) ON CONFLICT(slug) DO UPDATE SET name = excluded.name",
                agg.repo_names.items(),
            )

    def summarise(self, start: date, end: date, top_packages: int = 50) -> Summary:
        days = day_range(start, end)
        index = {d.isoformat(): i for i, d in enumerate(days)}
        window = (start.isoformat(), end.isoformat())
        q = self.db.execute

        policy_meta = {slug: (name, version, bool(terminal), seen) for slug, name, version, terminal, seen in q("SELECT * FROM policies")}
        repo_names = dict(q("SELECT slug, name FROM repos"))

        def buckets(sql: str, label_of) -> dict[str, Bucket]:
            out: dict[str, Bucket] = {}
            for key, day, matched, n in q(sql, window):
                b = out.get(key)
                if b is None:
                    b = out[key] = Bucket(key, label_of(key), daily_matched=[0] * len(days), daily_unmatched=[0] * len(days))
                if matched:
                    b.matched += n
                    b.daily_matched[index[day]] += n
                else:
                    b.unmatched += n
                    b.daily_unmatched[index[day]] += n
            return out

        group = "SELECT {col}, day, matched, SUM(n) FROM counts WHERE day BETWEEN ? AND ? GROUP BY 1, 2, 3"
        by_total = lambda b: (-b.total, b.label)  # noqa: E731

        policies = buckets(group.format(col="policy"), lambda k: policy_meta.get(k, (k,))[0])
        for slug, b in policies.items():
            if slug in policy_meta:
                _, version, terminal, seen = policy_meta[slug]
                b.last_seen = seen
                b.extra.update(version=version, terminal=terminal)
        repos = buckets(group.format(col="repo"), lambda k: repo_names.get(k, k))
        formats = buckets(group.format(col="format"), lambda k: k)

        daily = [Bucket(d.isoformat(), d.isoformat()) for d in days]
        for day, matched, n in q("SELECT day, matched, SUM(n) FROM counts WHERE day BETWEEN ? AND ? GROUP BY 1, 2", window):
            if matched:
                daily[index[day]].matched = n
            else:
                daily[index[day]].unmatched = n

        packages = []
        top = q(
            """SELECT format, name, SUM(n) FROM packages WHERE day BETWEEN ? AND ?
               GROUP BY 1, 2 ORDER BY 3 DESC, 2 LIMIT ?""",
            (*window, top_packages),
        ).fetchall()
        for fmt, name, n in top:
            b = Bucket(f"{fmt}:{name}", name, matched=n, daily_matched=[0] * len(days), daily_unmatched=[0] * len(days))
            pol = set()
            for day, policy, count in q(
                "SELECT day, policy, n FROM packages WHERE day BETWEEN ? AND ? AND format = ? AND name = ?",
                (*window, fmt, name),
            ):
                b.daily_matched[index[day]] += count
                pol.add(policy_meta.get(policy, (policy,))[0])
            b.extra.update(format=fmt, policies=sorted(pol))
            packages.append(b)

        evaluations = q("SELECT COALESCE(SUM(evaluations), 0) FROM days WHERE day BETWEEN ? AND ?", window).fetchone()[0]
        unique_packages = q("SELECT COUNT(DISTINCT pkg) FROM package_days WHERE day BETWEEN ? AND ?", window).fetchone()[0]
        matched = sum(b.matched for b in daily)
        unmatched = sum(b.unmatched for b in daily)

        return Summary(
            start=start,
            end=end,
            days=days,
            daily=daily,
            policies=sorted(policies.values(), key=by_total),
            repositories=sorted(repos.values(), key=by_total),
            formats=sorted(formats.values(), key=by_total),
            packages=packages,
            total=matched + unmatched,
            matched=matched,
            unmatched=unmatched,
            evaluations=evaluations,
            unique_packages=unique_packages,
            trend_pct=trend([b.total for b in daily]),
        )
