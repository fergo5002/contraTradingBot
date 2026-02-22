"""
LLM-based trade signal extraction.

Sends a filtered Reddit post to Claude (claude-sonnet-4-6) and asks it to
return a structured JSON signal describing what the author is doing and how
confident we are that this is a real trade signal.

Applies sentiment inversion if config mode == "against":
  long  → short   |   short → long
  call  → put     |   put   → call
"""

import json
import time
from dataclasses import dataclass, field
from typing import Optional

import anthropic

import db
from logger import get_logger

logger = get_logger(__name__)

# ─── Data models ──────────────────────────────────────────────────────────────

@dataclass
class OptionDetails:
    expiry: str            # YYYY-MM-DD
    strike: float          # Strike price in USD
    contract_type: str     # "call" or "put"  (post-inversion)


@dataclass
class TradeSignal:
    ticker: str
    asset_type: str          # "stock" | "crypto" | "option"
    direction: str           # "long" | "short"  (post-inversion if mode == against)
    raw_direction: str       # what the LLM saw before inversion
    confidence: float        # 0.0 – 1.0
    reasoning: str
    option_details: Optional[OptionDetails]
    post_id: str
    signal_id: Optional[int] = field(default=None)  # set after DB save


# ─── Claude prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a financial signal extraction AI. Your job is to read a Reddit post and decide
whether it contains a clear, actionable trade signal.

Respond ONLY with valid JSON — no markdown fences, no extra text.

Schema:
{
  "ticker":        "<symbol>",         // e.g. "AAPL", "BTC", "GME"
  "asset_type":    "stock"|"crypto"|"option",
  "direction":     "long"|"short",     // what the POST AUTHOR is doing
  "confidence":    0.0-1.0,            // how confident you are this is a real signal
  "reasoning":     "<brief explanation>",
  "option_details": {                  // ONLY when asset_type is "option", else null
    "expiry":         "YYYY-MM-DD",
    "strike":         0.0,
    "contract_type":  "call"|"put"
  }
}

Rules:
1. "direction" = what the POST AUTHOR is doing, NOT your recommendation.
   long/buy/bull/calls → "long".   short/put/bear/sell → "short".
2. confidence thresholds:
   - 0.9+  : post explicitly states a position with a specific ticker
   - 0.7-0.9: strong implication of a directional trade with clear ticker
   - 0.5-0.7: ticker present but direction or intent is ambiguous
   - <0.5  : meme, vague, no ticker, or unrelated
3. Use the most widely-accepted ticker symbol (e.g. "BTC" not "BITCOIN").
4. For stocks, use the exchange ticker (e.g. "NVDA" not "NVIDIA").
5. If multiple tickers are mentioned, pick the PRIMARY one the author is trading.
6. If you cannot identify a real trade signal, set confidence to 0.0 and
   ticker to "UNKNOWN".
7. Never invent a ticker that is not in the post.
"""

_USER_TEMPLATE = """\
Subreddit: r/{subreddit}
Title: {title}

Body:
{body}
"""

# ─── Retry helper ──────────────────────────────────────────────────────────────

def _with_retry(fn, max_attempts: int = 3, base_delay: float = 2.0):
    """Call *fn()* up to *max_attempts* times with exponential back-off."""
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except anthropic.RateLimitError as exc:
            if attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning("Claude rate limit hit, retrying in %.1fs (attempt %d)", delay, attempt)
            time.sleep(delay)
        except anthropic.APIStatusError as exc:
            if attempt == max_attempts or exc.status_code < 500:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning("Claude API %d error, retrying in %.1fs", exc.status_code, delay)
            time.sleep(delay)


# ─── Main class ────────────────────────────────────────────────────────────────

class SignalParser:
    def __init__(self, config: dict) -> None:
        self._mode: str = config.get("mode", "against").lower()
        self._min_confidence: float = float(config.get("min_confidence", 0.7))
        self._markets_enabled: list = [m.lower() for m in config.get("markets_enabled", ["stocks"])]
        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
        logger.info(
            "SignalParser ready | mode=%s | min_confidence=%.2f | markets=%s",
            self._mode, self._min_confidence, self._markets_enabled,
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def parse(self, post) -> Optional[TradeSignal]:
        """
        Send *post* to Claude, parse the JSON response, apply inversion if needed,
        and return a TradeSignal.  Returns None if the post should not generate a trade.
        """
        raw_json = self._call_claude(post)
        if raw_json is None:
            return None

        signal = self._parse_response(raw_json, post.post_id)
        if signal is None:
            return None

        # ── Confidence gate ───────────────────────────────────────────────────
        if signal.confidence < self._min_confidence:
            logger.info(
                "Signal discarded (confidence %.2f < %.2f): %s",
                signal.confidence, self._min_confidence, signal.ticker,
            )
            return None

        # ── Market gate ───────────────────────────────────────────────────────
        asset_key = signal.asset_type  # "stock", "crypto", "option"
        # Normalise: config uses "stocks" (plural) but LLM returns "stock"
        enabled = {m.rstrip("s") for m in self._markets_enabled}
        if asset_key not in enabled:
            logger.info(
                "Signal discarded (asset_type '%s' not in markets_enabled=%s): %s",
                asset_key, self._markets_enabled, signal.ticker,
            )
            return None

        # ── Sentiment inversion ───────────────────────────────────────────────
        if self._mode == "against":
            signal = self._invert(signal)

        # ── Persist to DB ─────────────────────────────────────────────────────
        signal_id = db.save_signal(
            post_id=signal.post_id,
            ticker=signal.ticker,
            asset_type=signal.asset_type,
            raw_direction=signal.raw_direction,
            final_direction=signal.direction,
            confidence=signal.confidence,
            reasoning=signal.reasoning,
        )
        signal.signal_id = signal_id
        logger.info(
            "Signal saved [id=%d]: %s %s (%s) conf=%.2f",
            signal_id, signal.ticker, signal.direction, signal.asset_type, signal.confidence,
        )
        return signal

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _call_claude(self, post) -> Optional[str]:
        user_content = _USER_TEMPLATE.format(
            subreddit=post.subreddit,
            title=post.title,
            body=(post.body or "")[:4000],  # cap to keep tokens reasonable
        )
        try:
            response = _with_retry(lambda: self._client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            ))
            return response.content[0].text
        except Exception as exc:
            logger.error("Claude API call failed: %s", exc, exc_info=True)
            return None

    @staticmethod
    def _parse_response(raw_json: str, post_id: str) -> Optional[TradeSignal]:
        try:
            data = json.loads(raw_json.strip())
        except json.JSONDecodeError as exc:
            logger.warning("JSON parse error from Claude: %s | raw=%s", exc, raw_json[:200])
            return None

        ticker = str(data.get("ticker", "UNKNOWN")).upper().strip()
        if ticker in ("UNKNOWN", "N/A", "", "NULL"):
            return None

        asset_type = str(data.get("asset_type", "stock")).lower()
        if asset_type not in ("stock", "crypto", "option"):
            asset_type = "stock"

        raw_direction = str(data.get("direction", "long")).lower()
        if raw_direction not in ("long", "short"):
            raw_direction = "long"

        confidence = float(data.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))

        reasoning = str(data.get("reasoning", ""))

        option_details: Optional[OptionDetails] = None
        if asset_type == "option":
            od = data.get("option_details") or {}
            try:
                option_details = OptionDetails(
                    expiry=str(od.get("expiry", "")),
                    strike=float(od.get("strike", 0.0)),
                    contract_type=str(od.get("contract_type", "call")).lower(),
                )
            except (TypeError, ValueError):
                logger.warning("Could not parse option_details for %s", ticker)
                option_details = None

        return TradeSignal(
            ticker=ticker,
            asset_type=asset_type,
            direction=raw_direction,   # will be inverted later if mode == against
            raw_direction=raw_direction,
            confidence=confidence,
            reasoning=reasoning,
            option_details=option_details,
            post_id=post_id,
        )

    @staticmethod
    def _invert(signal: TradeSignal) -> TradeSignal:
        """Flip direction (long↔short) and option contract type (call↔put)."""
        direction_map = {"long": "short", "short": "long"}
        contract_map = {"call": "put", "put": "call"}

        new_direction = direction_map.get(signal.direction, signal.direction)
        new_option_details = None

        if signal.option_details is not None:
            new_contract = contract_map.get(
                signal.option_details.contract_type,
                signal.option_details.contract_type,
            )
            new_option_details = OptionDetails(
                expiry=signal.option_details.expiry,
                strike=signal.option_details.strike,
                contract_type=new_contract,
            )

        return TradeSignal(
            ticker=signal.ticker,
            asset_type=signal.asset_type,
            direction=new_direction,
            raw_direction=signal.raw_direction,
            confidence=signal.confidence,
            reasoning=signal.reasoning,
            option_details=new_option_details,
            post_id=signal.post_id,
            signal_id=signal.signal_id,
        )
