"""
Position lifecycle management.

Responsibilities
─────────────────
  1. Gate new trades: enforce max_open_positions, max_position_size_usd,
     and "one position per ticker" rules before forwarding to TradeExecutor.
  2. Periodic position checks (runs in a background thread):
       - Refresh unrealised P&L for each open position using live Alpaca prices.
       - Auto-close positions that have exceeded holding_period_days.
  3. Deduplicate tickers: a signal for a ticker already in an open position
     is discarded with a clear log message.
"""

import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import db
from logger import get_logger
from signal_parser import TradeSignal
from trade_executor import TradeExecutor

logger = get_logger(__name__)

# How often (seconds) the background thread checks positions
_POSITION_CHECK_INTERVAL = 300  # 5 minutes


class PositionManager:
    def __init__(self, config: dict, executor: TradeExecutor) -> None:
        self._max_positions: int = int(config.get("max_open_positions", 10))
        self._max_usd: float = float(config.get("max_position_size_usd", 500))
        self._holding_days: int = int(config.get("holding_period_days", 7))
        self._executor = executor
        self._lock = threading.Lock()   # Serialise position opens to avoid races

    # ── Public API ─────────────────────────────────────────────────────────────

    def maybe_open_position(self, signal: TradeSignal, executor: TradeExecutor) -> bool:
        """
        Validate all limits and, if they pass, open the position.
        Returns True if a trade was submitted (or queued).
        """
        with self._lock:
            # 1. Already have a position in this ticker?
            existing = db.get_open_trade_for_ticker(signal.ticker)
            if existing is not None:
                logger.info(
                    "Skipping %s — already have an open position (trade_id=%d)",
                    signal.ticker, existing["id"],
                )
                return False

            # 2. Check for a recent signal for this ticker (deduplication window)
            if db.has_recent_signal_for_ticker(signal.ticker, hours=24):
                logger.info(
                    "Skipping %s — already generated a signal within 24 h",
                    signal.ticker,
                )
                return False

            # 3. Max open positions cap
            open_count = db.count_open_positions()
            if open_count >= self._max_positions:
                logger.warning(
                    "Max open positions (%d) reached — skipping %s",
                    self._max_positions, signal.ticker,
                )
                return False

            # 4. Execute
            logger.info(
                "Opening position: %s %s (%s) max_usd=$%.0f",
                signal.direction.upper(), signal.ticker, signal.asset_type, self._max_usd,
            )
            return executor.execute(signal)

    def run_periodic_checks(self, shutdown_event: threading.Event) -> None:
        """
        Background thread: refresh P&L and auto-close stale positions.
        Runs until *shutdown_event* is set.
        """
        logger.info("Position manager background thread started")
        while not shutdown_event.is_set():
            try:
                self._refresh_pnl()
                self._auto_close_stale()
            except Exception as exc:
                logger.error("Error in periodic position check: %s", exc, exc_info=True)
            # Sleep in small increments so we respond to shutdown quickly
            for _ in range(_POSITION_CHECK_INTERVAL):
                if shutdown_event.is_set():
                    break
                time.sleep(1)
        logger.info("Position manager background thread stopped")

    # ── Internal ───────────────────────────────────────────────────────────────

    def _refresh_pnl(self) -> None:
        """Update current_price and unrealised P&L for every open position."""
        trades = db.get_open_trades()
        if not trades:
            return
        for trade in trades:
            ticker = trade["ticker"]
            asset_type = trade["asset_type"] or "stock"
            entry = float(trade["entry_price"] or 0)
            qty = float(trade["qty"] or 0)
            direction = trade["direction"]

            current = self._executor.get_current_price(ticker, asset_type)
            if current is None or current <= 0:
                continue

            if direction == "long":
                pnl = (current - entry) * qty
            else:  # short
                pnl = (entry - current) * qty

            db.update_trade_price(trade["id"], current_price=current, pnl=pnl)

        logger.debug("Refreshed P&L for %d open position(s)", len(trades))

    def _auto_close_stale(self) -> None:
        """Close any position that has been open longer than holding_period_days."""
        cutoff = datetime.utcnow() - timedelta(days=self._holding_days)
        trades = db.get_open_trades()

        for trade in trades:
            opened_at_str = trade.get("opened_at") or ""
            if not opened_at_str:
                continue
            try:
                opened_at = datetime.fromisoformat(opened_at_str)
            except ValueError:
                continue

            if opened_at < cutoff:
                ticker = trade["ticker"]
                asset_type = trade["asset_type"] or "stock"
                logger.info(
                    "Auto-closing stale position: %s (opened %s, %d days old)",
                    ticker, opened_at_str[:10], self._holding_days,
                )
                closed = self._executor.close_position(ticker, asset_type)
                if closed:
                    # Fetch final price for P&L calculation
                    current = self._executor.get_current_price(ticker, asset_type) or 0.0
                    entry = float(trade["entry_price"] or 0)
                    qty = float(trade["qty"] or 0)
                    direction = trade["direction"]
                    if direction == "long":
                        final_pnl = (current - entry) * qty
                    else:
                        final_pnl = (entry - current) * qty
                    db.close_trade(trade["id"], current_price=current, pnl=final_pnl)
                    logger.info(
                        "Position closed: %s | final P&L = $%.2f", ticker, final_pnl
                    )

    def get_summary(self) -> dict:
        """Return a dict suitable for the dashboard."""
        trades = db.get_open_trades()
        total_pnl = db.get_total_pnl()
        unrealised = sum(float(t.get("pnl") or 0) for t in trades)
        return {
            "open_count": len(trades),
            "total_realised_pnl": total_pnl,
            "total_unrealised_pnl": unrealised,
            "trades": trades,
        }
