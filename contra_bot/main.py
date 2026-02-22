"""
ContraBot — entry point and main orchestration loop.

Startup sequence
────────────────
  1. Load .env and config.yaml
  2. Initialise SQLite database
  3. Initialise all components (Reddit, filter, parser, executor, position manager)
  4. Print Rich dashboard (open positions, P&L, config summary)
  5. Start position-manager background thread
  6. Poll Reddit every poll_interval_seconds:
       post → filter → LLM parse → position check → execute → log
  7. Graceful shutdown on CTRL+C (SIGINT) or SIGTERM

Error handling
──────────────
  All exceptions inside the main loop are caught and logged; the loop
  continues running.  Unrecoverable startup errors exit with code 1.
"""

import signal as signal_module
import sys
import threading
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import db
from filters import PostFilter
from logger import get_logger
from position_manager import PositionManager
from reddit_monitor import RedditMonitor
from signal_parser import SignalParser
from trade_executor import TradeExecutor

logger = get_logger(__name__)
console = Console()


# ─── Config loading ────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        logger.error("config.yaml not found at %s", config_path.resolve())
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─── Rich dashboard ────────────────────────────────────────────────────────────

def print_dashboard(config: dict, summary: dict) -> None:
    open_count = summary["open_count"]
    realised = summary["total_realised_pnl"]
    unrealised = summary["total_unrealised_pnl"]
    trades = summary["trades"]

    mode_colour = "red" if config["mode"] == "against" else "green"

    # ── Header panel ──────────────────────────────────────────────────────────
    pnl_colour = "green" if realised >= 0 else "red"
    unr_colour = "green" if unrealised >= 0 else "red"
    console.print(
        Panel(
            f"  Mode: [{mode_colour} bold]{config['mode'].upper()}[/{mode_colour} bold]"
            f"   |   Subreddits: [cyan]{', '.join(config['subreddits'])}[/cyan]"
            f"   |   Markets: [yellow]{', '.join(config['markets_enabled'])}[/yellow]\n"
            f"  Open Positions: [cyan]{open_count}/{config['max_open_positions']}[/cyan]"
            f"   |   Realised P&L: [{pnl_colour}]${realised:+.2f}[/{pnl_colour}]"
            f"   |   Unrealised: [{unr_colour}]${unrealised:+.2f}[/{unr_colour}]",
            title="[bold blue]ContraBot — Contra-Sentiment Paper Trader[/bold blue]",
            subtitle="[dim]Paper trading only — no real money at risk[/dim]",
            border_style="blue",
        )
    )

    # ── Config summary ─────────────────────────────────────────────────────────
    cfg_table = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
    cfg_table.add_column("Key", style="dim")
    cfg_table.add_column("Value")
    cfg_table.add_row("Min confidence", str(config.get("min_confidence", 0.7)))
    cfg_table.add_row("Max position size", f"${config.get('max_position_size_usd', 500)}")
    cfg_table.add_row("Poll interval", f"{config.get('poll_interval_seconds', 60)}s")
    cfg_table.add_row("Holding period", f"{config.get('holding_period_days', 7)} days")
    cfg_table.add_row("Min author karma", str(config.get("min_author_karma", 100)))
    console.print(cfg_table)

    # ── Open positions table ───────────────────────────────────────────────────
    if trades:
        pos_table = Table(title="Open Positions", box=box.SIMPLE_HEAVY, show_lines=False)
        pos_table.add_column("Ticker", style="cyan bold")
        pos_table.add_column("Dir", style="white")
        pos_table.add_column("Type", style="dim")
        pos_table.add_column("Qty", justify="right")
        pos_table.add_column("Entry", justify="right")
        pos_table.add_column("Current", justify="right")
        pos_table.add_column("Unr. P&L", justify="right")
        pos_table.add_column("Opened", style="dim")

        for t in trades:
            pnl = float(t.get("pnl") or 0)
            pnl_str = f"[green]${pnl:+.2f}[/green]" if pnl >= 0 else f"[red]${pnl:+.2f}[/red]"
            pos_table.add_row(
                t["ticker"],
                t["direction"],
                t.get("asset_type", "?"),
                f"{float(t['qty']):.4g}" if t.get("qty") else "?",
                f"${float(t['entry_price']):.2f}" if t.get("entry_price") else "?",
                f"${float(t['current_price']):.2f}" if t.get("current_price") else "?",
                pnl_str,
                (t.get("opened_at") or "")[:16],
            )
        console.print(pos_table)
    else:
        console.print("[dim]  No open positions.[/dim]")

    console.print()


# ─── Pipeline ─────────────────────────────────────────────────────────────────

def run_pipeline(
    post,
    config: dict,
    post_filter: PostFilter,
    signal_parser: SignalParser,
    trade_executor: TradeExecutor,
    position_manager: PositionManager,
) -> None:
    """
    Run a single Reddit post through the full pipeline:
      filter → LLM parse → position check → execute → DB audit trail
    """
    # ── 1. Filter ─────────────────────────────────────────────────────────────
    filter_result = post_filter.filter(post)

    # Save post record regardless of filter outcome
    db.save_post(
        subreddit=post.subreddit,
        post_id=post.post_id,
        title=post.title,
        body=post.body,
        author=post.author,
        created_utc=post.created_utc,
        upvotes=post.upvotes,
        awards=post.awards,
        filter_passed=filter_result.passed,
        filter_reason=filter_result.reason,
    )

    if not filter_result.passed:
        logger.debug("FILTERED [%s]: %s", filter_result.reason, post.title[:60])
        return

    logger.info(
        "PASS filter | r/%s | [%s]: %s",
        post.subreddit,
        post.post_id,
        post.title[:80],
    )

    # ── 2. LLM signal parse ────────────────────────────────────────────────────
    signal = signal_parser.parse(post)
    if signal is None:
        logger.info("No actionable signal from post %s", post.post_id)
        return

    logger.info(
        "SIGNAL: %s %s (%s) conf=%.2f | %s",
        signal.direction.upper(), signal.ticker, signal.asset_type,
        signal.confidence, signal.reasoning[:100],
    )

    # ── 3. Position check + execution ─────────────────────────────────────────
    opened = position_manager.maybe_open_position(signal, trade_executor)
    if opened:
        logger.info("TRADE submitted: %s %s", signal.direction.upper(), signal.ticker)
    else:
        logger.info("TRADE skipped for %s (see above)", signal.ticker)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    # ── Environment + config ──────────────────────────────────────────────────
    load_dotenv()
    config = load_config()

    # ── Database ──────────────────────────────────────────────────────────────
    db.init_db()

    # ── Components ────────────────────────────────────────────────────────────
    try:
        reddit_monitor = RedditMonitor(config)
        post_filter = PostFilter(config)
        signal_parser = SignalParser(config)
        trade_executor = TradeExecutor(config)
        position_manager = PositionManager(config, trade_executor)
    except EnvironmentError as exc:
        logger.error("Startup failed: %s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.error("Unexpected startup error: %s", exc, exc_info=True)
        sys.exit(1)

    # ── Initial dashboard ─────────────────────────────────────────────────────
    print_dashboard(config, position_manager.get_summary())

    # ── Graceful shutdown ──────────────────────────────────────────────────────
    shutdown_event = threading.Event()

    def _on_signal(sig, frame):
        console.print("\n[yellow]Shutdown signal received — finishing current work...[/yellow]")
        logger.info("Shutdown requested (signal %s)", sig)
        shutdown_event.set()

    signal_module.signal(signal_module.SIGINT, _on_signal)
    signal_module.signal(signal_module.SIGTERM, _on_signal)

    # ── Background position manager ────────────────────────────────────────────
    bg_thread = threading.Thread(
        target=position_manager.run_periodic_checks,
        args=(shutdown_event,),
        daemon=True,
        name="PositionManager",
    )
    bg_thread.start()

    # ── Main polling loop ──────────────────────────────────────────────────────
    poll_interval = int(config.get("poll_interval_seconds", 60))
    logger.info(
        "ContraBot running | subreddits=%s | interval=%ds",
        config["subreddits"], poll_interval,
    )

    last_poll: float = 0.0
    last_dashboard: float = 0.0
    DASHBOARD_INTERVAL = 600  # Reprint dashboard every 10 minutes

    while not shutdown_event.is_set():
        now = time.time()

        # Reprint dashboard periodically
        if now - last_dashboard >= DASHBOARD_INTERVAL:
            last_dashboard = now
            print_dashboard(config, position_manager.get_summary())

        # Main poll
        if now - last_poll >= poll_interval:
            last_poll = now

            # Submit any orders that were queued while market was closed
            try:
                trade_executor.submit_pending_orders()
            except Exception as exc:
                logger.error("Error submitting pending orders: %s", exc)

            # Fetch and process new Reddit posts
            try:
                posts = reddit_monitor.fetch_new_posts()
                if posts:
                    logger.info("Processing %d new post(s)...", len(posts))
                for post in posts:
                    if shutdown_event.is_set():
                        break
                    try:
                        run_pipeline(
                            post, config, post_filter, signal_parser,
                            trade_executor, position_manager,
                        )
                    except Exception as exc:
                        logger.error(
                            "Pipeline error for post %s: %s", post.post_id, exc, exc_info=True
                        )
                    # Brief pause between posts to be kind to APIs
                    time.sleep(0.5)

            except Exception as exc:
                logger.error("Reddit fetch error: %s", exc, exc_info=True)

        # Sleep briefly to keep CPU idle between polls
        time.sleep(1)

    # ── Cleanup ────────────────────────────────────────────────────────────────
    bg_thread.join(timeout=10)
    console.print("\n[bold green]ContraBot stopped cleanly.[/bold green]")
    logger.info("ContraBot stopped")


if __name__ == "__main__":
    main()
