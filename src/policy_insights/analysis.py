"""Fold decision logs into per-day aggregates, and turn those into the numbers the views render."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta


class DayAgg:
    """Running aggregates for one UTC day of decision logs.

    Raw logs are folded in page by page and then discarded, so memory stays
    proportional to the number of distinct policies/repos/packages rather than
    the number of logs.
    """

    def __init__(self, day: date) -> None:
        self.day = day
        self.logs = 0
        # (policy, repository, format, matched) -> count
        self.counts: Counter[tuple[str, str, str, bool]] = Counter()
        # (format, package name, policy) -> matched count
        self.packages: Counter[tuple[str, str, str]] = Counter()
        self.correlations: set[str] = set()
        self.package_ids: set[str] = set()
        self.repo_names: dict[str, str] = {}
        # policy -> (name, version, terminal, last_seen)
        self.policies: dict[str, tuple[str, int, bool, str]] = {}

    def add(self, logs: list[dict]) -> None:
        counts, packages, policies = self.counts, self.packages, self.policies
        correlations, package_ids, repo_names = self.correlations, self.package_ids, self.repo_names
        for log in logs:
            policy = log.get("policy_slug_perm") or "?"
            repo = log.get("repository_slug_perm") or "?"
            fmt = (log.get("package_format") or "?").upper()
            matched = bool(log.get("match"))
            counts[(policy, repo, fmt, matched)] += 1
            if matched:
                packages[(fmt, log.get("package_name") or "?", policy)] += 1
            correlations.add(log.get("correlation_id"))
            package_ids.add(log.get("package_slug_perm"))
            repo_names[repo] = log.get("repository_name") or repo
            seen = log.get("started_at") or ""
            current = policies.get(policy)
            if current is None or seen > current[3]:
                policies[policy] = (
                    log.get("policy_name") or policy,
                    log.get("policy_version") or 0,
                    bool(log.get("policy_is_terminal")),
                    seen,
                )
        self.logs += len(logs)

    def merge(self, other: DayAgg) -> None:
        self.logs += other.logs
        self.counts.update(other.counts)
        self.packages.update(other.packages)
        self.correlations |= other.correlations
        self.package_ids |= other.package_ids
        self.repo_names.update(other.repo_names)
        for policy, meta in other.policies.items():
            current = self.policies.get(policy)
            if current is None or meta[3] > current[3]:
                self.policies[policy] = meta


@dataclass
class Bucket:
    """Matched / unmatched counts for some grouping (a day, policy, repo...)."""

    key: str
    label: str
    matched: int = 0
    unmatched: int = 0
    daily_matched: list[int] = field(default_factory=list)
    daily_unmatched: list[int] = field(default_factory=list)
    last_seen: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.matched + self.unmatched

    @property
    def match_rate(self) -> float:
        return self.matched / self.total if self.total else 0.0

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "matched": self.matched,
            "unmatched": self.unmatched,
            "total": self.total,
            "match_rate": round(self.match_rate, 4),
            "daily_matched": self.daily_matched,
            "daily_unmatched": self.daily_unmatched,
            "last_seen": self.last_seen,
            **self.extra,
        }


@dataclass
class Summary:
    start: date
    end: date
    days: list[date]
    daily: list[Bucket]
    policies: list[Bucket]
    repositories: list[Bucket]
    formats: list[Bucket]
    packages: list[Bucket]
    total: int
    matched: int
    unmatched: int
    evaluations: int
    unique_packages: int
    trend_pct: float | None

    @property
    def match_rate(self) -> float:
        return self.matched / self.total if self.total else 0.0

    @property
    def avg_per_day(self) -> float:
        return self.total / len(self.days) if self.days else 0.0

    @property
    def busiest_day(self) -> Bucket | None:
        return max(self.daily, key=lambda b: b.total, default=None)

    @property
    def silent_policies(self) -> list[Bucket]:
        """Policies that were evaluated but never matched in the window."""
        return [p for p in self.policies if p.matched == 0]


def day_range(start: date, end: date) -> list[date]:
    return [start + timedelta(n) for n in range((end - start).days + 1)]


def trend(series: list[int]) -> float | None:
    """Percent change in the daily average, second half of the window vs first."""
    if len(series) < 4:
        return None
    half = len(series) // 2
    first = sum(series[:half]) / half
    second = sum(series[-half:]) / half
    if first == 0:
        return None
    return (second - first) / first * 100


def top_matching(summary: Summary, n: int) -> list[Bucket]:
    return sorted(summary.policies, key=lambda b: (-b.matched, -b.total, b.label))[:n]


def top_noisy(summary: Summary, n: int) -> list[Bucket]:
    return sorted(summary.policies, key=lambda b: (-b.unmatched, b.match_rate, b.label))[:n]
