"""
Reddit post ingestion via PRAW.

- Polls configured subreddits for *new* posts (not comments).
- Deduplicates against the SQLite posts table so previously seen posts
  are never re-processed, even across restarts.
- Extracts post metadata into PostData dataclasses.
"""

import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

import praw
from praw.models import Submission
from prawcore.exceptions import PrawcoreException

from db import is_post_processed
from logger import get_logger

logger = get_logger(__name__)


@dataclass
class PostData:
    post_id: str
    subreddit: str
    title: str
    body: str
    url: str
    author: str
    author_karma: Optional[int]
    created_utc: float
    upvotes: int
    awards: int
    is_self: bool  # True = text post, False = link post


class RedditMonitor:
    def __init__(self, config: dict) -> None:
        self._subreddits: List[str] = config["subreddits"]
        self._posts_per_poll: int = int(config.get("posts_per_poll", 25))
        self._reddit = self._build_client()

    # ── Public API ─────────────────────────────────────────────────────────────

    def fetch_new_posts(self) -> List[PostData]:
        """
        Fetch the latest posts from all configured subreddits.
        Returns only posts that have not been seen before.
        """
        results: List[PostData] = []
        for subreddit_name in self._subreddits:
            try:
                posts = self._fetch_subreddit(subreddit_name)
                results.extend(posts)
                # Small polite delay between subreddits
                time.sleep(0.5)
            except PrawcoreException as exc:
                logger.warning("Reddit API error for r/%s: %s", subreddit_name, exc)
            except Exception as exc:
                logger.error("Unexpected error fetching r/%s: %s", subreddit_name, exc, exc_info=True)
        return results

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _build_client(self) -> praw.Reddit:
        client_id = os.getenv("REDDIT_CLIENT_ID")
        client_secret = os.getenv("REDDIT_CLIENT_SECRET")
        user_agent = os.getenv("REDDIT_USER_AGENT", "ContraBot/1.0")

        if not client_id or not client_secret:
            raise EnvironmentError(
                "REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET must be set in the environment."
            )

        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=user_agent,
            # Read-only mode — we never post to Reddit
            read_only=True,
        )
        logger.info("Reddit client initialised (read-only)")
        return reddit

    def _fetch_subreddit(self, subreddit_name: str) -> List[PostData]:
        subreddit = self._reddit.subreddit(subreddit_name)
        posts: List[PostData] = []

        for submission in subreddit.new(limit=self._posts_per_poll):
            if is_post_processed(submission.id):
                continue  # Already in our DB

            post = self._extract(submission, subreddit_name)
            if post is not None:
                posts.append(post)
                logger.debug(
                    "New post from r/%s [%s]: %s",
                    subreddit_name,
                    submission.id,
                    submission.title[:80],
                )

        logger.info("r/%s → %d new posts", subreddit_name, len(posts))
        return posts

    @staticmethod
    def _extract(submission: Submission, subreddit_name: str) -> Optional[PostData]:
        """Convert a PRAW Submission into a PostData, handling missing fields gracefully."""
        try:
            author_name = str(submission.author) if submission.author else "[deleted]"
            author_karma: Optional[int] = None
            try:
                if submission.author:
                    author_karma = submission.author.link_karma + submission.author.comment_karma
            except Exception:
                pass  # Karma fetch is best-effort

            # Count gildings as a proxy for "awards"
            awards = sum(submission.gildings.values()) if submission.gildings else 0

            body = (submission.selftext or "").strip()
            # Treat removed/deleted bodies as empty
            if body in ("[removed]", "[deleted]"):
                body = ""

            return PostData(
                post_id=submission.id,
                subreddit=subreddit_name,
                title=submission.title or "",
                body=body,
                url=submission.url or "",
                author=author_name,
                author_karma=author_karma,
                created_utc=float(submission.created_utc),
                upvotes=int(submission.score or 0),
                awards=awards,
                is_self=bool(submission.is_self),
            )
        except Exception as exc:
            logger.warning("Failed to extract submission %s: %s", submission.id, exc)
            return None
