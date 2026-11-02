"""Render a decision-log summary to the terminal with Rich."""

from __future__ import annotations

import math

from rich import box
from rich.console import Console, Group
from rich.columns import Columns
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .analysis import Bucket, Summary, top_matching, top_noisy

MATCHED = "#498af6"  # Cloudsmith blue (dark-surface step)
UNMATCHED = "#de7056"  # Cloudsmith salmon, darkened for contrast
MUTED = "grey50"
SPARKS = "▁▂▃▄▅▆▇█"
LOWER = " ▁▂▃▄▅▆▇█"
LEFT = " ▏▎▍▌▋▊▉█"


def _fmt(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:,.0f}"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def sparkline(values: list[int], style: str) -> Text:
    peak = max(values, default=0)
    text = Text()
    for v in values:
        if v == 0:
            text.append("·", MUTED)
        else:
            text.append(SPARKS[min(7, math.ceil(v / peak * 8) - 1)], style)
    return text


def hbar(value: float, peak: float, width: int, style: str) -> Text:
    """A horizontal bar with eighth-block resolution."""
    if peak <= 0 or value <= 0:
        return Text("")
    eighths = max(1, round(value / peak * width * 8))
    return Text("█" * (eighths // 8) + LEFT[eighths % 8].strip(), style)


def split_bar(matched: int, unmatched: int, peak: int, width: int) -> Text:
    """Matched and unmatched side by side, scaled to a shared peak."""
    if peak <= 0:
        return Text("")
    m = round(matched / peak * width)
    u = round(unmatched / peak * width)
    if matched and not m:
        m = 1
    if unmatched and not u:
        u = 1
    return Text("█" * m, MATCHED) + Text("█" * u, UNMATCHED)


def _shown(matched: int, unmatched: int, view: str) -> tuple[int, int]:
    """Zero out whichever outcome the view hides."""
    return (0 if view == "unmatched" else matched, 0 if view == "matched" else unmatched)


def _legend(view: str) -> Text:
    legend = Text("  ")
    if view != "unmatched":
        legend.append("█ matched  ", MATCHED)
    if view != "matched":
        legend.append("█ unmatched", UNMATCHED)
    return legend


def _bucketise(buckets: list[Bucket], max_cols: int) -> tuple[list[tuple[str, int, int]], int]:
    """Fold days together so the chart fits in `max_cols` columns."""
    size = max(1, math.ceil(len(buckets) / max_cols))
    out = []
    for i in range(0, len(buckets), size):
        chunk = buckets[i : i + size]
        out.append((chunk[0].label, sum(b.matched for b in chunk), sum(b.unmatched for b in chunk)))
    return out, size


def _x_labels(cols: list[tuple[str, int, int]], col_w: int, plot_w: int) -> str:
    """First, middle and last dates, dropping whichever won't fit."""
    label_w = len(cols[0][0])
    chars = [" "] * max(plot_w, label_w)
    picks = [(0, 0)]
    if plot_w >= 2 * label_w + 2 and len(cols) > 1:
        picks.append((len(cols) - 1, plot_w - label_w))
    if plot_w >= 3 * label_w + 4 and len(cols) > 2:
        mid = len(cols) // 2
        picks.append((mid, min(max(mid * col_w - label_w // 2, label_w + 2), plot_w - 2 * label_w - 2)))
    for idx, pos in picks:
        chars[pos : pos + label_w] = cols[idx][0]
    return "".join(chars)


def trend_chart(summary: Summary, width: int, height: int = 12, view: str = "all") -> Group:
    axis_w = 7
    cols, size = _bucketise(summary.daily, max(1, (width - axis_w - 4)))
    cols = [(label, *_shown(m, u, view)) for label, m, u in cols]
    col_w = max(1, min(4, (width - axis_w - 4) // max(1, len(cols))))
    gap = col_w >= 3
    bar_w = col_w - 1 if gap else col_w
    peak = max((m + u for _, m, u in cols), default=0) or 1

    lines = []
    for row in range(height, 0, -1):
        if row == height:
            label = _fmt(peak)
        elif row == math.ceil(height / 2):
            label = _fmt(peak * row / height)
        else:
            label = ""
        line = Text(f"{label:>{axis_w - 2}} │", MUTED)
        for _, m, u in cols:
            mh, th = m / peak * height, (m + u) / peak * height
            if m + u and th < 1 / 8:
                th = 1 / 8  # keep tiny days visible
            lo = row - 1
            if mh >= row:
                cell = Text("█" * bar_w, MATCHED)
            elif th >= row:
                if mh > lo:  # matched ends inside this cell, unmatched carries on above
                    cell = Text(LOWER[round((mh - lo) * 8)] * bar_w, f"{MATCHED} on {UNMATCHED}")
                else:
                    cell = Text("█" * bar_w, UNMATCHED)
            elif th > lo:
                frac = max(1, round((th - lo) * 8))
                top = UNMATCHED if u and th > mh else MATCHED
                cell = Text(LOWER[frac] * bar_w, top)
            else:
                cell = Text(" " * bar_w)
            line.append_text(cell)
            if gap:
                line.append(" ")
        lines.append(line)

    plot_w = len(cols) * col_w
    lines.append(Text(" " * (axis_w - 1) + "└" + "─" * plot_w, MUTED))
    lines.append(Text(" " * axis_w + _x_labels(cols, col_w, plot_w), MUTED))

    legend = _legend(view)
    if size > 1:
        legend.append(f"   (each column = {size} days)", MUTED)
    lines.append(legend)
    return Group(*lines)


def kpi(title: str, value: str, sub: str = "", style: str = "bold") -> Panel:
    body = Text(value, style)
    if sub:
        body.append(f"\n{sub}", MUTED)
    return Panel(body, title=title, title_align="left", box=box.ROUNDED, padding=(0, 1))


def render(summary: Summary, console: Console, *, workspace: str, top: int = 10, source: str = "", view: str = "all") -> None:
    width = min(console.width, 160)

    sub = f"{summary.start} → {summary.end}  ·  {len(summary.days)} days"
    if source:
        sub += f"  ·  data: {source}"
    if view != "all":
        sub += f"  ·  charts: {view} only"
    console.print(
        Panel(
            Text(f"Policy Insights — {workspace}", "bold") + Text(f"\n{sub}", MUTED),
            box=box.HEAVY,
            width=width,
        )
    )

    if not summary.total:
        console.print("[yellow]No decision logs in this window.[/]")
        return

    trend = summary.trend_pct
    trend_txt = "n/a" if trend is None else f"{'▲' if trend >= 0 else '▼'} {abs(trend):.0f}%"
    busiest = summary.busiest_day
    tiles = [
                kpi("Decision logs", _fmt(summary.total), f"{summary.avg_per_day:,.1f} / day avg"),
                kpi("Matched", _fmt(summary.matched), f"{_pct(summary.match_rate)} match rate", f"bold {MATCHED}"),
                kpi("Unmatched", _fmt(summary.unmatched), f"{_pct(1 - summary.match_rate)} of logs", f"bold {UNMATCHED}"),
                kpi("Evaluations", _fmt(summary.evaluations), f"{_fmt(summary.unique_packages)} packages"),
                kpi("Policies", str(len(summary.policies)), f"{len(summary.silent_policies)} never matched"),
                kpi("Volume trend", trend_txt, "vs first half"),
                kpi("Busiest day", _fmt(busiest.total) if busiest else "-", busiest.label if busiest else ""),
    ]
    per_row = max(1, min(len(tiles), width // 20))
    grid = Table.grid(expand=True, padding=(0, 1))
    for _ in range(per_row):
        grid.add_column(ratio=1)
    for i in range(0, len(tiles), per_row):
        row = tiles[i : i + per_row]
        grid.add_row(*row, *[""] * (per_row - len(row)))
    console.print(grid, width=width)

    console.print(
        Panel(trend_chart(summary, width - 4, view=view), title="Logs per day", title_align="left", width=width)
    )
    console.print(_daily_table(summary, width, view))
    console.print(_policy_table(summary, top, width, noisy=False))
    console.print(_policy_table(summary, top, width, noisy=True))
    console.print(Columns([_group_table("Repositories", summary.repositories, top, view), _group_table("Formats", summary.formats, top, view)]))
    console.print(_packages_table(summary, top))


def _daily_table(summary: Summary, width: int, view: str = "all", last: int = 14) -> Table:
    rows = summary.daily[-last:]
    peak = max((sum(_shown(b.matched, b.unmatched, view)) for b in rows), default=0)
    t = Table(title=f"Last {len(rows)} days", title_justify="left", box=box.SIMPLE_HEAD, expand=False)
    t.add_column("Day")
    t.add_column("Logs", justify="right")
    t.add_column("Matched", justify="right", style=MATCHED)
    t.add_column("Unmatched", justify="right", style=UNMATCHED)
    t.add_column("Match %", justify="right")
    t.add_column("", no_wrap=True)
    bar_w = max(10, min(50, width - 60))
    for b in rows:
        t.add_row(b.label, f"{b.total:,}", f"{b.matched:,}", f"{b.unmatched:,}", _pct(b.match_rate), split_bar(*_shown(b.matched, b.unmatched, view), peak, bar_w))
    return t


def _policy_table(summary: Summary, top: int, width: int, *, noisy: bool) -> Table:
    if noisy:
        rows = top_noisy(summary, top)
        title = "Noisiest policies — most unmatched evaluations"
        peak, style, value = max((p.unmatched for p in rows), default=0), UNMATCHED, "unmatched"
    else:
        rows = top_matching(summary, top)
        title = "Most active policies — by matches"
        peak, style, value = max((p.matched for p in rows), default=0), MATCHED, "matched"

    t = Table(title=title, title_justify="left", box=box.SIMPLE_HEAD)
    t.add_column("#", justify="right", style=MUTED)
    t.add_column("Policy", max_width=36, no_wrap=True, overflow="ellipsis")
    t.add_column(value.capitalize(), justify="right", style=style, min_width=9)
    t.add_column("", no_wrap=True)
    t.add_column("Evaluated", justify="right")
    t.add_column("Match %", justify="right")
    t.add_column(f"Daily {value}", no_wrap=True)
    t.add_column("Notes", style=MUTED, no_wrap=True)
    bar_w = max(8, min(30, width - 120))
    for i, p in enumerate(rows, 1):
        notes = []
        if p.extra.get("terminal"):
            notes.append("terminal")
        if p.matched == 0:
            notes.append("never matched")
        n = getattr(p, value)
        series = p.daily_unmatched if noisy else p.daily_matched
        t.add_row(
            str(i),
            p.label,
            f"{n:,}",
            hbar(n, peak, bar_w, style),
            f"{p.total:,}",
            _pct(p.match_rate),
            sparkline(series[-30:], style),
            ", ".join(notes),
        )
    return t


def _group_table(title: str, buckets: list[Bucket], top: int, view: str = "all") -> Table:
    if view == "all":
        rows = buckets[:top]
    else:
        rows = sorted((b for b in buckets if getattr(b, view)), key=lambda b: -getattr(b, view))[:top]
    peak = max((sum(_shown(b.matched, b.unmatched, view)) for b in rows), default=0)
    t = Table(title=title, title_justify="left", box=box.SIMPLE_HEAD)
    t.add_column("Name", max_width=24, no_wrap=True, overflow="ellipsis")
    if view == "all":
        t.add_column("Logs", justify="right")
    else:
        t.add_column(view.capitalize(), justify="right", style=MATCHED if view == "matched" else UNMATCHED)
    t.add_column("Match %", justify="right")
    t.add_column("", no_wrap=True)
    for b in rows:
        count = b.total if view == "all" else getattr(b, view)
        t.add_row(b.label, f"{count:,}", _pct(b.match_rate), split_bar(*_shown(b.matched, b.unmatched, view), peak, 20))
    if not rows:
        t.add_row(f"[dim]no {view}[/]", "", "", "")
    return t


def _packages_table(summary: Summary, top: int) -> Table:
    rows = summary.packages[:top]
    peak = max((b.matched for b in rows), default=0)
    t = Table(title="Most-matched packages", title_justify="left", box=box.SIMPLE_HEAD)
    t.add_column("Package", max_width=32, no_wrap=True, overflow="ellipsis")
    t.add_column("Format")
    t.add_column("Matches", justify="right", style=MATCHED)
    t.add_column("", no_wrap=True)
    t.add_column("Matched by", style=MUTED, max_width=50, overflow="ellipsis", no_wrap=True)
    for b in rows:
        t.add_row(b.label, b.extra.get("format", ""), f"{b.matched:,}", hbar(b.matched, peak, 16, MATCHED), ", ".join(b.extra.get("policies", [])))
    if not rows:
        t.add_row("[dim]no matches[/]", "", "", "", "")
    return t
