# ContraBot — Contra-Sentiment Paper Trader

A Python bot that monitors Reddit subreddits, uses Claude (claude-sonnet-4-6) to
extract trade signals from posts, inverts the crowd sentiment, and executes paper
trades via Alpaca's paper trading API.

> **Paper trading only.** No real money is at risk.

---

## How It Works

```
Reddit post
    │
    ▼
[filters.py]  ──── Discard sports bets, memes, no-ticker posts, low-karma authors
    │
    ▼
[signal_parser.py]  ── Claude reads the post, returns structured JSON signal
    │                   {ticker, direction, confidence, asset_type, ...}
    │
    ▼
[position_manager.py]  ── Enforce position limits, deduplicate tickers
    │
    ▼
[trade_executor.py]  ── Submit paper order to Alpaca
    │
    ▼
[db.py]  ── Every step written to SQLite for full audit trail
```

---

## Setup

### 1. Install dependencies

```bash
cd contra_bot
pip install -r requirements.txt
```

### 2. Get Reddit API credentials

1. Log into Reddit and go to <https://www.reddit.com/prefs/apps>
2. Click **"create another app"**
3. Select type: **script**
4. Name it anything (e.g. `ContraBot`)
5. Set redirect URI to `http://localhost:8080` (not actually used)
6. Click **Create app**
7. Copy the **client ID** (shown under the app name) and **secret**

### 3. Get Anthropic API key

1. Go to <https://console.anthropic.com/>
2. Create an account and navigate to **API Keys**
3. Click **Create Key** and copy it

### 4. Get Alpaca paper trading credentials

1. Sign up at <https://app.alpaca.markets/>
2. In the top-right, switch the environment to **Paper**
3. Go to **API Keys** in the left sidebar
4. Click **Generate New Key** and copy both the key and secret

### 5. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials:

```env
REDDIT_CLIENT_ID=your_reddit_client_id
REDDIT_CLIENT_SECRET=your_reddit_client_secret
REDDIT_USER_AGENT=ContraBot/1.0 by YourRedditUsername

ANTHROPIC_API_KEY=sk-ant-...

ALPACA_API_KEY=your_alpaca_key
ALPACA_SECRET_KEY=your_alpaca_secret
```

### 6. Review config

Edit `config.yaml` to set your preferences (subreddits, mode, position limits, etc.).

---

## Running the Bot

```bash
cd contra_bot
python main.py
```

Press **CTRL+C** to stop gracefully.

The bot will:
- Print a startup dashboard with open positions and total P&L
- Poll Reddit every `poll_interval_seconds` (default: 60)
- Log every post, filter result, signal, and trade to `logs/contra_bot.log`
- Store full audit trail in `contra_bot.db` (SQLite)

---

## Configuration Reference (`config.yaml`)

| Key | Default | Description |
|-----|---------|-------------|
| `subreddits` | `[wallstreetbets]` | List of subreddits to monitor |
| `mode` | `against` | `against` = invert signal, `with` = follow signal |
| `markets_enabled` | `[stocks, crypto]` | Asset classes to trade (`stocks`, `crypto`, `options`) |
| `min_confidence` | `0.7` | Claude confidence threshold (0.0–1.0) |
| `max_position_size_usd` | `500` | Max USD per trade |
| `max_open_positions` | `10` | Max simultaneously open positions |
| `poll_interval_seconds` | `60` | Seconds between Reddit polls |
| `holding_period_days` | `7` | Auto-close positions after N days |
| `min_author_karma` | `100` | Minimum karma to process a post |
| `posts_per_poll` | `25` | Posts fetched per subreddit per poll |

---

## Adding New Subreddits

Edit `config.yaml`:

```yaml
subreddits:
  - wallstreetbets
  - stocks
  - investing
  - options
  - CryptoCurrency
```

The bot will monitor all subreddits in parallel on each poll cycle.

---

## Switching Between "Against" and "With" Mode

Edit `config.yaml`:

```yaml
# Bet against the crowd (contrarian)
mode: against

# Follow the crowd (momentum)
mode: with
```

In `against` mode, all signals are inverted before execution:
- `long → short`
- `short → long`
- `call → put`
- `put → call`

---

## Inspecting the Database

The bot stores everything in `contra_bot.db`. You can inspect it with any SQLite viewer
or the `sqlite3` CLI:

```bash
sqlite3 contra_bot.db

# See all posts processed
SELECT post_id, subreddit, title, filter_passed FROM posts ORDER BY processed_at DESC LIMIT 20;

# See all signals generated
SELECT ticker, asset_type, raw_direction, final_direction, confidence FROM signals ORDER BY created_at DESC;

# See all trades and P&L
SELECT ticker, direction, qty, entry_price, pnl, status FROM trades ORDER BY opened_at DESC;
```

---

## Options Support

Options trading requires Alpaca options account approval. Enable in `config.yaml`:

```yaml
markets_enabled:
  - stocks
  - crypto
  - options
```

If your account doesn't have options approval, the bot will log a message and skip
options signals without crashing.

---

## Logs

Logs are written to `logs/contra_bot.log` (rotating, 10 MB max, 5 files kept).
The console shows INFO and above; the file captures full DEBUG detail.

---

## Project Structure

```
contra_bot/
├── main.py               # Entry point, orchestration loop
├── config.yaml           # All user-configurable settings
├── reddit_monitor.py     # Reddit post ingestion via PRAW
├── signal_parser.py      # LLM-based post interpreter (Claude API)
├── trade_executor.py     # Alpaca paper trading execution
├── position_manager.py   # Tracks open positions, P&L, exposure limits
├── filters.py            # Pre-LLM filtering (sports, memes, no ticker, karma)
├── logger.py             # Structured logging to file + console
├── db.py                 # SQLite database for posts, signals, trades
├── requirements.txt
├── .env.example
└── README.md
```
