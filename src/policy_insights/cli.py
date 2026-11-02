"""Command-line entry point."""

from __future__ import annotations

import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from . import demo
from .analysis import DayAgg, day_range
from .auth import AuthError, resolve_credential, resolve_workspace
from .client import ApiError, FetchStats, fetch_days
from .dashboard import write_dashboard
from .store import Store, cache_path, is_settled
from .terminal import render

app = typer.Typer(add_completion=False, rich_markup_mode="rich")
console = Console()
err = Console(stderr=True)


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(f"expected YYYY-MM-DD, got {value!r}")


@app.command()
def main(
    workspace: Annotated[Optional[str], typer.Option("--workspace", "-w", help="Workspace slug. Defaults to $CLOUDSMITH_WORKSPACE or the CLI's config.ini.")] = None,
    days: Annotated[int, typer.Option("--days", "-d", min=1, help="Look back this many days (ignored if --since is set).")] = 30,
    since: Annotated[Optional[str], typer.Option(help="Start date, YYYY-MM-DD (UTC, inclusive).")] = None,
    until: Annotated[Optional[str], typer.Option(help="End date, YYYY-MM-DD (UTC, inclusive). Defaults to today.")] = None,
    policy: Annotated[Optional[str], typer.Option(help="Only this policy (slug_perm).")] = None,
    repository: Annotated[Optional[str], typer.Option("--repo", help="Only this repository (slug_perm).")] = None,
    package_format: Annotated[Optional[list[str]], typer.Option("--format", help="Only these package formats; repeatable (e.g. --format npm --format python).")] = None,
    package_name: Annotated[Optional[str], typer.Option("--package", help="Only this package name.")] = None,
    html: Annotated[Optional[Path], typer.Option("--html", help="Write an HTML dashboard to this path.")] = None,
    open_html: Annotated[bool, typer.Option("--open", help="Open the HTML dashboard in a browser once written.")] = False,
    no_terminal: Annotated[bool, typer.Option("--no-terminal", help="Skip the terminal report (useful with --html).")] = False,
    top: Annotated[int, typer.Option(min=1, help="How many rows in the top-N tables and charts.")] = 10,
    api_key: Annotated[Optional[str], typer.Option("--api-key", "-k", envvar=None, help="API key. Defaults to $CLOUDSMITH_API_KEY, then the Cloudsmith CLI's credentials.")] = None,
    profile: Annotated[Optional[str], typer.Option("--profile", "-P", help="Cloudsmith CLI profile to take credentials from.")] = None,
    credentials_file: Annotated[Optional[Path], typer.Option(help="Path to a Cloudsmith CLI credentials.ini.")] = None,
    page_size: Annotated[int, typer.Option(min=1, max=1000, help="Logs per API request.")] = 1000,
    workers: Annotated[int, typer.Option("--workers", "-j", min=1, max=64, help="Parallel API requests. Lower it if you hit rate limits.")] = 8,
    refresh: Annotated[bool, typer.Option("--refresh", help="Re-fetch every day in the window, ignoring the cache.")] = False,
    offline: Annotated[bool, typer.Option("--offline", help="Use only cached days; don't call the API.")] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Don't read or write the on-disk cache.")] = False,
    use_demo: Annotated[bool, typer.Option("--demo", help="Use generated sample data instead of calling the API.")] = False,
) -> None:
    """Visualise Cloudsmith policy decision logs: volume per day, matched vs unmatched,
    the most active policies and the noisiest ones.

    Finished days are cached on disk (~/.cache/policy-insights), so re-runs only fetch
    days not seen before plus today."""
    end_day = _parse_day(until) or datetime.now(timezone.utc).date()
    start_day = _parse_day(since) or end_day - timedelta(days=days - 1)
    if start_day > end_day:
        raise typer.BadParameter("--since is after --until")
    window = day_range(start_day, end_day)

    filters = {
        "policy": policy,
        "repository": repository,
        "format": package_format,
        "package": package_name,
    }

    if use_demo:
        workspace = workspace or "demo-workspace"
        store = Store()
        _ingest(store, demo.generate(days=len(window) - 1))
        source = "demo data"
    else:
        workspace = resolve_workspace(workspace, profile)
        if not workspace:
            err.print("[red]No workspace given.[/] Pass --workspace or set CLOUDSMITH_WORKSPACE.")
            raise typer.Exit(2)
        store = Store() if no_cache else Store(cache_path(workspace, filters))
        cached = set() if refresh else store.complete_days()
        missing = [d for d in window if d not in cached]
        source = f"{len(window) - len(missing)} of {len(window)} days cached"

        if offline:
            if missing:
                err.print(f"[yellow]{len(missing)} day(s) in the window aren't cached and will show as empty.[/]")
            source = "cache only"
        elif missing:
            try:
                credential = resolve_credential(api_key, profile, credentials_file)
            except AuthError as exc:
                err.print(f"[red]{exc}[/]")
                raise typer.Exit(2)
            source += f" · auth via {credential.source}"
            _fetch(store, credential, workspace, missing, filters, page_size, workers)

    summary = store.summarise(start_day, end_day)
    store.close()

    if not no_terminal:
        render(summary, console, workspace=workspace, top=top, source=source)

    if html:
        path = write_dashboard(summary, html, workspace=workspace, top=top, filters=filters)
        err.print(f"[green]Dashboard written to[/] {path.resolve()}")
        if open_html:
            webbrowser.open(path.resolve().as_uri())


def _fetch(store: Store, credential, workspace: str, days: list[date], filters: dict, page_size: int, workers: int) -> None:
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("days · {task.fields[logs]:,} logs · {task.fields[rate]}"),
        TimeElapsedColumn(),
        console=err,
        transient=True,
    )
    task = progress.add_task(f"Fetching {workspace}", total=len(days), logs=0, rate="")
    started = perf_counter()

    def on_progress(stats: FetchStats) -> None:
        elapsed = perf_counter() - started
        progress.update(task, completed=stats.days_done, logs=stats.logs, rate=f"{stats.logs / elapsed:,.0f}/s" if elapsed > 1 else "")

    def on_day(agg) -> None:
        store.save_day(agg, complete=is_settled(agg.day))

    with progress:
        try:
            stats = fetch_days(
                credential,
                workspace,
                days,
                on_day,
                policy=filters["policy"],
                repository=filters["repository"],
                package_formats=filters["format"],
                package_name=filters["package"],
                page_size=page_size,
                workers=workers,
                on_progress=on_progress,
            )
        except ApiError as exc:
            progress.stop()
            err.print(f"[red]{exc}[/]")
            raise typer.Exit(1)
        except KeyboardInterrupt:
            progress.stop()
            err.print("[yellow]Interrupted. Days already fetched are cached, so re-running resumes from there.[/]")
            raise typer.Exit(130)

    elapsed = perf_counter() - started
    line = (
        f"Fetched {stats.logs:,} logs for {stats.days_done} day(s) in {elapsed:.1f}s "
        f"({stats.logs / max(elapsed, 0.001):,.0f}/s) · {stats.requests:,} requests, "
        f"avg {stats.avg_latency:.2f}s each, {workers} workers"
    )
    if stats.throttled:
        line += f" · [yellow]rate-limited {stats.throttled}x, paused {stats.paused_seconds:.0f}s[/] (try a lower -j)"
    elif stats.requests > workers * 4:
        line += " · no rate limiting (a higher -j may be faster)"
    err.print(f"[dim]{line}[/]")


def _ingest(store: Store, logs: list[dict]) -> None:
    """Load raw logs (demo data) into a store, bucketed by UTC day."""
    by_day: dict[date, DayAgg] = {}
    for log in logs:
        day = date.fromisoformat(log["started_at"][:10])
        by_day.setdefault(day, DayAgg(day)).add([log])
    for agg in by_day.values():
        store.save_day(agg, complete=True)


if __name__ == "__main__":
    app()
