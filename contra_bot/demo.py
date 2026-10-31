"""
Offline demo: runs the real filters and the real signal flip on a handful of
sample posts. No Reddit, Anthropic or Alpaca keys needed.

The one step it fakes is Claude's reading of each post. Those readings are
written out below so you can see the shape the bot expects back.

    python demo.py
"""

import time

from rich.console import Console

from filters import PostFilter
from reddit_monitor import PostData
from signal_parser import SignalParser, TradeSignal

console = Console(highlight=False)


def post(pid, title, body="", karma=4200, is_self=True, url=""):
    return PostData(
        post_id=pid, subreddit="wallstreetbets", title=title, body=body, url=url,
        author="sample", author_karma=karma, created_utc=time.time(),
        upvotes=0, awards=0, is_self=is_self,
    )


# Sample posts, and what Claude would plausibly say about the ones that get through.
SAMPLES = [
    (post("p1", "GME is going to the moon", "Loaded 50 shares this morning. Not selling until 400."),
     TradeSignal("GME", "stock", "long", "long", 0.86, "Author bought shares and says they will hold", None, "p1")),
    (post("p2", "Parlay locked in", "Chiefs -3.5 and the over. Easiest money of the season."), None),
    (post("p3", "me after buying the dip", is_self=False, url="https://i.redd.it/dip.png"), None),
    (post("p4", "Sold every last ETH", "Dumped the lot at the open. This chart only goes one way now."),
     TradeSignal("ETH", "crypto", "short", "short", 0.78, "Author sold their whole position and expects a fall",
                 None, "p4")),
    (post("p5", "BTC to 200k by Christmas", "Trust me on this one.", karma=12), None),
]


def describe(sig: TradeSignal) -> str:
    return f"{sig.direction.upper()} {sig.ticker}"


def main() -> None:
    gate = PostFilter({"min_author_karma": 100})
    console.print("[bold]r/wallstreetbets[/bold]  [dim]mode: against[/dim]\n")
    for p, reading in SAMPLES:
        time.sleep(0.6)
        console.print(f"[white]\"{p.title}\"[/white]")
        result = gate.filter(p)
        if not result.passed:
            console.print(f"  [dim]skipped: {result.reason}[/dim]\n")
            continue
        flipped = SignalParser._invert(reading)
        console.print(f"  [yellow]crowd:[/yellow]  {describe(reading)}  [dim]({reading.confidence:.0%} sure)[/dim]")
        console.print(f"  [bold red]contra:[/bold red] [bold]{describe(flipped)}[/bold]  [dim]paper order, about $500[/dim]\n")


if __name__ == "__main__":
    main()
