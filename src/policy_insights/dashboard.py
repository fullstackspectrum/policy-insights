"""Render a decision-log summary as a self-contained HTML dashboard."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

from .analysis import Summary, top_matching, top_noisy


def build_payload(summary: Summary, *, workspace: str, top: int, filters: dict, view: str = "all") -> dict:
    busiest = summary.busiest_day
    return {
        "meta": {
            "workspace": workspace,
            "start": summary.start.isoformat(),
            "end": summary.end.isoformat(),
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "filters": {k: v for k, v in filters.items() if v},
            "top": top,
            "view": view,
        },
        "kpis": {
            "total": summary.total,
            "matched": summary.matched,
            "unmatched": summary.unmatched,
            "match_rate": summary.match_rate,
            "evaluations": summary.evaluations,
            "packages": summary.unique_packages,
            "policies": len(summary.policies),
            "silent_policies": len(summary.silent_policies),
            "avg_per_day": summary.avg_per_day,
            "trend_pct": summary.trend_pct,
            "busiest_day": busiest.label if busiest else None,
            "busiest_total": busiest.total if busiest else 0,
        },
        "days": [d.isoformat() for d in summary.days],
        "daily": [b.to_dict() for b in summary.daily],
        "policies": [b.to_dict() for b in summary.policies],
        "top_matching": [b.key for b in top_matching(summary, top)],
        "top_noisy": [b.key for b in top_noisy(summary, top)],
        "repositories": [b.to_dict() for b in summary.repositories],
        "formats": [b.to_dict() for b in summary.formats],
        "packages": [b.to_dict() for b in summary.packages[:50]],
    }


def write_dashboard(
    summary: Summary, path: Path, *, workspace: str, top: int = 10, filters: dict | None = None, view: str = "all"
) -> Path:
    payload = build_payload(summary, workspace=workspace, top=top, filters=filters or {}, view=view)
    template = files("policy_insights").joinpath("templates/dashboard.html").read_text(encoding="utf-8")
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    page = template.replace("__TITLE__", html.escape(f"Policy Insights · {workspace}")).replace("__PAYLOAD__", data)
    path.write_text(page, encoding="utf-8")
    return path
