"""
Post filtering.

A post must pass ALL filters to be forwarded to the LLM signal parser.
Filters run cheaply (no API calls) to reduce cost and latency.
"""

import re
from dataclasses import dataclass
from typing import Any

from logger import get_logger

logger = get_logger(__name__)

# ─── Sports / gambling vocabulary ─────────────────────────────────────────────
_SPORTS_KEYWORDS: frozenset[str] = frozenset(
    {
        # Leagues & events
        "nfl", "nba", "mlb", "nhl", "mls", "ufc", "ncaa",
        "premier league", "la liga", "bundesliga", "serie a", "ligue 1",
        "champions league", "europa league", "super bowl", "march madness",
        "world cup", "playoffs", "stanley cup", "nba finals",
        # Betting terminology
        "parlay", "moneyline", "point spread", "over/under", "over under",
        "handicap", "teaser", "prop bet", "futures bet", "live bet",
        "draftkings", "fanduel", "pointsbet", "betmgm", "caesars sportsbook",
        "bet365", "barstool sportsbook", "wynn bet", "fanatics betting",
        "fantasy football", "fantasy basketball", "fantasy baseball",
        "daily fantasy", "dfs", "sports betting",
        # Generic sports signals
        "quarterback", "touchdown", "home run", "slam dunk", "hat trick",
        "mvp award", "first round pick", "draft pick",
    }
)

# ─── Common crypto names so we can recognise them as valid instruments ────────
_CRYPTO_NAMES: frozenset[str] = frozenset(
    {
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "dogecoin",
        "doge", "cardano", "ada", "ripple", "xrp", "avalanche", "avax",
        "polkadot", "dot", "chainlink", "link", "litecoin", "ltc",
        "uniswap", "uni", "polygon", "matic", "shiba", "shib", "pepe",
        "bnb", "binance", "tron", "trx", "near", "ftm", "fantom",
        "injective", "inj", "arbitrum", "arb", "optimism", "op",
    }
)

# Ticker pattern: optional $, then 1–5 uppercase letters (stock-like)
_TICKER_RE = re.compile(r"\$?[A-Z]{1,5}\b")

# Image/media URL extensions that signal a meme / link post
_IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".svg"}
)


@dataclass
class FilterResult:
    passed: bool
    reason: str


class PostFilter:
    def __init__(self, config: dict) -> None:
        self._min_karma: int = int(config.get("min_author_karma", 100))

    # ── Public entry point ────────────────────────────────────────────────────

    def filter(self, post: Any) -> FilterResult:
        """
        Run all filters against *post* (a PostData instance).
        Returns a FilterResult with passed=True only when every check passes.
        """
        checks = [
            self._check_sports,
            self._check_meme,
            self._check_financial_instrument,
            self._check_author_karma,
        ]
        for check in checks:
            result = check(post)
            if not result.passed:
                return result
        return FilterResult(passed=True, reason="all checks passed")

    # ── Individual filter checks ──────────────────────────────────────────────

    @staticmethod
    def _check_sports(post: Any) -> FilterResult:
        combined = f"{post.title} {post.body}".lower()
        for keyword in _SPORTS_KEYWORDS:
            if keyword in combined:
                return FilterResult(passed=False, reason=f"sports/gambling keyword: '{keyword}'")
        return FilterResult(passed=True, reason="no sports keywords")

    @staticmethod
    def _check_meme(post: Any) -> FilterResult:
        """Reject image-only posts with no meaningful text body."""
        if not post.is_self:
            # Link post — check if it points to an image with no body text
            url_lower = (post.url or "").lower()
            has_image_url = any(url_lower.endswith(ext) for ext in _IMAGE_EXTENSIONS)
            body_text = (post.body or "").strip()
            if has_image_url and not body_text:
                return FilterResult(passed=False, reason="image-only link post (meme)")
        else:
            # Text/self post — reject if body is essentially empty
            body_text = (post.body or "").strip()
            if not body_text or len(body_text) < 20:
                if not _has_instrument_in_text(post.title):
                    return FilterResult(passed=False, reason="self-post with no body text")
        return FilterResult(passed=True, reason="not a meme post")

    @staticmethod
    def _check_financial_instrument(post: Any) -> FilterResult:
        combined = f"{post.title} {post.body}"
        if _has_instrument_in_text(combined):
            return FilterResult(passed=True, reason="financial instrument found")
        return FilterResult(passed=False, reason="no identifiable financial instrument")

    def _check_author_karma(self, post: Any) -> FilterResult:
        karma = getattr(post, "author_karma", None)
        if karma is None:
            # If we couldn't fetch karma, allow it through (don't block on missing data)
            return FilterResult(passed=True, reason="karma unavailable, allowing")
        if karma < self._min_karma:
            return FilterResult(
                passed=False,
                reason=f"author karma {karma} below threshold {self._min_karma}",
            )
        return FilterResult(passed=True, reason=f"author karma {karma} OK")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _has_instrument_in_text(text: str) -> bool:
    """
    Return True if the text contains at least one recognisable financial instrument:
      - A $TICKER pattern (e.g. $AAPL, $GME)
      - A bare all-caps ticker of 1–5 letters that isn't a common English stop-word
      - A known crypto name or symbol
    """
    text_lower = text.lower()
    # Quick crypto name check
    for name in _CRYPTO_NAMES:
        if re.search(r"\b" + re.escape(name) + r"\b", text_lower):
            return True

    # $TICKER always counts
    if re.search(r"\$[A-Z]{1,5}\b", text):
        return True

    # Bare uppercase 2-5 letter word that is not a common stop-word
    _STOP_WORDS = frozenset(
        {
            "I", "A", "AN", "THE", "AND", "OR", "BUT", "FOR", "NOR", "SO", "YET",
            "AT", "BY", "IN", "OF", "ON", "TO", "UP", "AS", "IS", "IT", "BE",
            "DO", "GO", "IF", "NO", "MY", "HE", "ME", "WE", "US", "AM", "VS",
            "TV", "PC", "OK", "AI", "IT", "HQ", "DD", "TL", "DR", "IMO", "LOL",
            "OMG", "WTF", "CEO", "CFO", "COO", "CTO", "SEC", "FED", "IPO",
        }
    )
    for match in _TICKER_RE.finditer(text):
        word = match.group().lstrip("$")
        if len(word) >= 2 and word not in _STOP_WORDS:
            return True

    return False
