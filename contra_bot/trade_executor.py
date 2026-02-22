"""
Alpaca paper-trading execution layer.

Supports:
  - Stocks   : market orders (queued if market is closed, submitted at open)
  - Crypto   : market orders (24/7, no market-hours restriction)
  - Options  : best-effort via Alpaca options API; skipped with a log message
               if the account doesn't support it

Crypto symbol normalisation
  The LLM returns bare symbols like "BTC" or "ETH".
  Alpaca expects "BTC/USD", "ETH/USD", etc.
  A mapping covers the most common coins; unknown ones get "/USD" appended.

Short-selling stocks on Alpaca paper trading is enabled by default.
Shorting crypto is NOT supported on Alpaca; short crypto signals are logged and skipped.
"""

import os
import time
from typing import Optional

from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoLatestQuoteRequest, StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetAssetsRequest,
)

import db
from logger import get_logger
from signal_parser import TradeSignal

logger = get_logger(__name__)

# ─── Crypto symbol mapping ─────────────────────────────────────────────────────

_CRYPTO_MAP: dict[str, str] = {
    "BTC": "BTC/USD", "BITCOIN": "BTC/USD",
    "ETH": "ETH/USD", "ETHEREUM": "ETH/USD",
    "SOL": "SOL/USD", "SOLANA": "SOL/USD",
    "DOGE": "DOGE/USD", "DOGECOIN": "DOGE/USD",
    "ADA": "ADA/USD", "CARDANO": "ADA/USD",
    "XRP": "XRP/USD", "RIPPLE": "XRP/USD",
    "AVAX": "AVAX/USD", "AVALANCHE": "AVAX/USD",
    "DOT": "DOT/USD", "POLKADOT": "DOT/USD",
    "LINK": "LINK/USD", "CHAINLINK": "LINK/USD",
    "LTC": "LTC/USD", "LITECOIN": "LTC/USD",
    "UNI": "UNI/USD", "UNISWAP": "UNI/USD",
    "MATIC": "MATIC/USD", "POLYGON": "MATIC/USD",
    "SHIB": "SHIB/USD", "SHIBA": "SHIB/USD",
    "BNB": "BNB/USD",
    "NEAR": "NEAR/USD",
    "FTM": "FTM/USD", "FANTOM": "FTM/USD",
    "INJ": "INJ/USD", "INJECTIVE": "INJ/USD",
    "ARB": "ARB/USD", "ARBITRUM": "ARB/USD",
    "OP": "OP/USD", "OPTIMISM": "OP/USD",
    "PEPE": "PEPE/USD",
}


def _to_alpaca_crypto_symbol(ticker: str) -> str:
    upper = ticker.upper()
    return _CRYPTO_MAP.get(upper, f"{upper}/USD")


def _to_alpaca_stock_symbol(ticker: str) -> str:
    return ticker.upper().replace("/", "")


# ─── Retry helper ──────────────────────────────────────────────────────────────

def _retry(fn, label: str, max_attempts: int = 3, base_delay: float = 1.5):
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning("%s failed (attempt %d): %s — retrying in %.1fs", label, attempt, exc, delay)
            time.sleep(delay)


# ─── Main class ────────────────────────────────────────────────────────────────

class TradeExecutor:
    def __init__(self, config: dict) -> None:
        api_key = os.getenv("ALPACA_API_KEY")
        secret_key = os.getenv("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise EnvironmentError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the environment."
            )

        self._max_usd: float = float(config.get("max_position_size_usd", 500))

        # Trading client (paper=True forces the paper-trading base URL)
        self._trading = TradingClient(api_key, secret_key, paper=True)

        # Data clients for price lookups (no auth required for crypto data)
        self._stock_data = StockHistoricalDataClient(api_key, secret_key)
        self._crypto_data = CryptoHistoricalDataClient()

        logger.info("TradeExecutor initialised (Alpaca paper trading)")

    # ── Public API ─────────────────────────────────────────────────────────────

    def is_market_open(self) -> bool:
        try:
            clock = self._trading.get_clock()
            return bool(clock.is_open)
        except Exception as exc:
            logger.warning("Could not fetch market clock: %s", exc)
            return False

    def get_current_price(self, ticker: str, asset_type: str) -> Optional[float]:
        """Return the latest ask price for a ticker, or None on failure."""
        try:
            if asset_type == "crypto":
                symbol = _to_alpaca_crypto_symbol(ticker)
                req = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
                quotes = self._crypto_data.get_crypto_latest_quote(req)
                quote = quotes.get(symbol)
                if quote:
                    return float(quote.ask_price or quote.bid_price)
            else:
                symbol = _to_alpaca_stock_symbol(ticker)
                req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
                quotes = self._stock_data.get_stock_latest_quote(req)
                quote = quotes.get(symbol)
                if quote:
                    return float(quote.ask_price or quote.bid_price)
        except Exception as exc:
            logger.warning("Could not fetch price for %s: %s", ticker, exc)
        return None

    def execute(self, signal: TradeSignal) -> bool:
        """
        Execute a trade for the given signal.

        For stock orders when the market is closed, the order is queued in the
        DB (pending_orders) and will be submitted at next market open via
        submit_pending_orders().

        Returns True if an order was submitted (or queued), False otherwise.
        """
        if signal.asset_type == "option":
            return self._execute_option(signal)
        if signal.asset_type == "crypto":
            return self._execute_crypto(signal)
        # Default: stock
        return self._execute_stock(signal)

    def submit_pending_orders(self) -> None:
        """Called from the main loop; submits queued stock orders if market is now open."""
        if not self.is_market_open():
            return
        pending = db.get_pending_orders()
        if not pending:
            return
        logger.info("Market is open — submitting %d pending order(s)", len(pending))
        for row in pending:
            try:
                self._submit_stock_order(
                    signal_id=row["signal_id"],
                    ticker=row["ticker"],
                    direction=row["direction"],
                    qty=row["qty"],
                    asset_type=row["asset_type"],
                )
                db.delete_pending_order(row["id"])
            except Exception as exc:
                logger.error("Failed to submit pending order %d: %s", row["id"], exc)

    def close_position(self, ticker: str, asset_type: str) -> bool:
        """Close an open Alpaca position by ticker."""
        try:
            symbol = (
                _to_alpaca_crypto_symbol(ticker)
                if asset_type == "crypto"
                else _to_alpaca_stock_symbol(ticker)
            )
            self._trading.close_position(symbol)
            logger.info("Closed Alpaca position: %s", symbol)
            return True
        except Exception as exc:
            logger.error("Failed to close position %s: %s", ticker, exc)
            return False

    # ── Stocks ─────────────────────────────────────────────────────────────────

    def _execute_stock(self, signal: TradeSignal) -> bool:
        ticker = _to_alpaca_stock_symbol(signal.ticker)
        direction = signal.direction  # "long" or "short"

        price = self.get_current_price(signal.ticker, "stock")
        if price is None or price <= 0:
            logger.warning("Could not get price for %s — skipping", ticker)
            return False

        qty = max(1, int(self._max_usd / price))

        if not self.is_market_open():
            # Queue for market open
            logger.info("Market closed — queuing stock order: %s %s x%d", direction, ticker, qty)
            db.save_pending_order(
                signal_id=signal.signal_id,
                ticker=ticker,
                direction=direction,
                qty=float(qty),
                asset_type="stock",
            )
            return True

        return self._submit_stock_order(
            signal_id=signal.signal_id,
            ticker=ticker,
            direction=direction,
            qty=float(qty),
            asset_type="stock",
        )

    def _submit_stock_order(
        self, signal_id: int, ticker: str, direction: str, qty: float, asset_type: str
    ) -> bool:
        side = OrderSide.BUY if direction == "long" else OrderSide.SELL

        def _submit():
            return self._trading.submit_order(
                MarketOrderRequest(
                    symbol=ticker,
                    qty=qty,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                )
            )

        try:
            order = _retry(_submit, f"stock order {ticker}")
            entry_price = self.get_current_price(ticker, asset_type) or 0.0
            db.save_trade(
                signal_id=signal_id,
                alpaca_order_id=str(order.id),
                ticker=ticker,
                direction=direction,
                asset_type=asset_type,
                qty=qty,
                entry_price=entry_price,
                status="open",
            )
            logger.info(
                "Stock order submitted ✓  %s %s x%.0f @ ~$%.2f  order_id=%s",
                side.value.upper(), ticker, qty, entry_price, order.id,
            )
            return True
        except Exception as exc:
            logger.error("Stock order failed for %s: %s", ticker, exc, exc_info=True)
            return False

    # ── Crypto ──────────────────────────────────────────────────────────────────

    def _execute_crypto(self, signal: TradeSignal) -> bool:
        symbol = _to_alpaca_crypto_symbol(signal.ticker)
        direction = signal.direction

        # Alpaca does not support short-selling crypto
        if direction == "short":
            logger.info(
                "Skipping short crypto signal for %s — Alpaca does not support crypto shorts",
                symbol,
            )
            return False

        price = self.get_current_price(signal.ticker, "crypto")
        if price is None or price <= 0:
            logger.warning("Could not get price for %s — skipping", symbol)
            return False

        # Fractional crypto quantities allowed; round to 6 decimal places
        qty = round(self._max_usd / price, 6)
        qty = max(qty, 1e-6)

        def _submit():
            return self._trading.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.GTC,
                )
            )

        try:
            order = _retry(_submit, f"crypto order {symbol}")
            db.save_trade(
                signal_id=signal.signal_id,
                alpaca_order_id=str(order.id),
                ticker=symbol,
                direction=direction,
                asset_type="crypto",
                qty=qty,
                entry_price=price,
                status="open",
            )
            logger.info(
                "Crypto order submitted ✓  BUY %s qty=%.6f @ ~$%.4f  order_id=%s",
                symbol, qty, price, order.id,
            )
            return True
        except Exception as exc:
            logger.error("Crypto order failed for %s: %s", symbol, exc, exc_info=True)
            return False

    # ── Options ─────────────────────────────────────────────────────────────────

    def _execute_option(self, signal: TradeSignal) -> bool:
        """
        Attempt to place an options order via Alpaca.

        Alpaca options trading requires explicit account approval and the
        alpaca-py OptionLeg / PlaceOptionOrderRequest classes.  If these are
        not available (account not approved or wrong SDK version) we log a
        clear message and return False rather than crashing.
        """
        if signal.option_details is None:
            logger.warning("Option signal for %s has no option_details — skipping", signal.ticker)
            return False

        od = signal.option_details
        ticker = _to_alpaca_stock_symbol(signal.ticker)
        contract_type = od.contract_type.upper()[0]  # "C" or "P"

        # Build OCC option symbol: AAPL240315C00200000
        try:
            expiry_compact = od.expiry.replace("-", "")[2:]  # "YYYYMMDD" → "YYMMDD"
            strike_int = int(round(od.strike * 1000))
            occ_symbol = f"{ticker}{expiry_compact}{contract_type}{strike_int:08d}"
        except Exception as exc:
            logger.warning("Could not build OCC symbol for %s: %s", signal.ticker, exc)
            return False

        side = OrderSide.BUY if signal.direction == "long" else OrderSide.SELL

        try:
            from alpaca.trading.requests import OptionLegRequest, PlaceOptionOrderRequest  # type: ignore[import]

            order_req = PlaceOptionOrderRequest(
                qty=1,
                type="market",
                time_in_force="day",
                legs=[OptionLegRequest(symbol=occ_symbol, side=side, ratio_qty=1)],
            )
            order = _retry(
                lambda: self._trading.submit_order(order_req),
                f"option order {occ_symbol}",
            )
            db.save_trade(
                signal_id=signal.signal_id,
                alpaca_order_id=str(order.id),
                ticker=occ_symbol,
                direction=signal.direction,
                asset_type="option",
                qty=1.0,
                entry_price=0.0,
                status="open",
            )
            logger.info(
                "Option order submitted ✓  %s %s  order_id=%s",
                side.value.upper(), occ_symbol, order.id,
            )
            return True
        except ImportError:
            logger.warning(
                "Options not supported in this version of alpaca-py — skipping %s", occ_symbol
            )
        except Exception as exc:
            logger.error("Option order failed for %s: %s", occ_symbol, exc, exc_info=True)
        return False
