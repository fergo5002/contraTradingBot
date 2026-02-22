# 🤖 Contra — Anti-Sentiment Trading Bot

> *If the internet is going long, we're going short.*

Contra is a configurable trading bot that monitors Reddit communities, interprets posts as trade signals using AI, and executes the opposing position via Alpaca's paper trading API. Built initially to fade r/wallstreetbets, it's designed to work with any subreddit and any sentiment direction.

---

## How It Works

1. **Monitor** — Polls configured subreddits for new posts (never comments)
2. **Filter** — Discards sports bets, memes, and non-actionable noise
3. **Interpret** — Sends posts to Claude AI to extract a structured trade signal (ticker, direction, confidence)
4. **Invert** — Flips the signal (long → short, call → put) if running in `against` mode
5. **Execute** — Submits the trade to Alpaca's paper trading environment
6. **Track** — Logs every post, signal, and trade to a local SQLite database with full audit trail

---

## Features

- 🔄 **Universal** — Point it at any subreddit, flip between `with` or `against` mode in config
- 📊 **Multi-market** — Supports US stocks, crypto, and options
- 🧠 **AI-powered signal parsing** — Uses Claude to interpret natural language posts into structured trades
- 🚫 **Smart filtering** — Automatically discards sports bets, image-only memes, and low-signal posts
- 💾 **Full audit trail** — Every post, filter result, signal, and trade stored in SQLite
- 📈 **Position management** — Enforces position limits, max exposure, and auto-closes stale trades
- 🖥️ **Live dashboard** — Startup summary of open positions and P&L via rich console output

---

## Stack

| Component | Tool |
|---|---|
| Reddit ingestion | PRAW |
| Signal parsing | Anthropic Claude API |
| Broker | Alpaca (paper trading) |
| Database | SQLite |
| Language | Python 3.10+ |

---

## Getting Started

### 1. Clone the repo

```bash
git clone https://github.com/fergo5002/contra.git
cd contra
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set up credentials

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

You'll need:

- **Reddit API credentials** — Create an app at [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps). Select "script", give it any name, and copy the client ID and secret.
- **Anthropic API key** — Get one at [console.anthropic.com](https://console.anthropic.com)
- **Alpaca paper trading keys** — Sign up at [alpaca.markets](https://alpaca.markets) and generate keys from the paper trading dashboard

### 4. Configure the bot

Edit `config.yaml` to set your subreddits, markets, confidence thresholds, and position limits:

```yaml
subreddits:
  - wallstreetbets
mode: against        # "against" or "with"
markets_enabled:
  - stocks
  - crypto
  - options
min_confidence: 0.7
max_position_size_usd: 500
max_open_positions: 10
```

### 5. Run

```bash
python main.py
```

---

## Project Structure

```
contra/
├── main.py               # Entry point and main loop
├── config.yaml           # All configuration
├── reddit_monitor.py     # Reddit post ingestion
├── signal_parser.py      # AI-powered trade signal extraction
├── trade_executor.py     # Alpaca order execution
├── position_manager.py   # Open position tracking and P&L
├── filters.py            # Sports bet / meme / noise filtering
├── db.py                 # SQLite database layer
├── logger.py             # Structured logging
├── .env.example          # Environment variable template
└── requirements.txt
```

---

## Adding More Subreddits

Just add them to `config.yaml`:

```yaml
subreddits:
  - wallstreetbets
  - stocks
  - investing
  - superstonk
```

Each subreddit is monitored independently. Posts are deduplicated per ticker to prevent the same trade being submitted multiple times.

---

## Notes

- This bot runs in **paper trading mode only** — no real money is ever at risk
- Only **posts** are used as signals, never comments
- Posts about sports betting (NFL, NBA, DraftKings, etc.) are automatically discarded
- A post must hit the configured confidence threshold before a trade is submitted

---

## License

MIT
