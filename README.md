# Barabot

Barabot is a Telegram product for finding relevant listings in selected chats.

The project is being rebuilt around product behavior. The previous inbox/outbox/command queue design and application schema have been removed intentionally. A minimal empty SQLite database connection remains so persistence can be designed from scratch after the Telegram event flow and user-facing behavior are defined.

## Current status

Implemented and tested:

- one CLI entry point: `python -m barabot`;
- resumable interactive and headless configuration;
- Telegram user-session authorization for the scraper through Telethon;
- embedding-provider configuration;
- BotFather token verification and private persistence;
- shared service lifecycle and graceful shutdown;
- a single-instance lock and PID file.
- an empty application database connection with no schema or migrations.

Not implemented:

- Telegram event handlers in `ScraperService`;
- listing extraction and matching;
- user subscriptions and chat selection;
- notifications from `BotService`;
- product persistence;
- communication between runtime services.

`ScraperService`, `ProcessorService`, and `BotService` remain lifecycle stubs. No application schema or queue architecture is currently selected.

## Runtime

The three services run in one process under `asyncio.TaskGroup` and share this lifecycle:

```python
async def start() -> None
async def run_forever(stop: asyncio.Event) -> None
async def stop() -> None
```

If one service fails, `TaskGroup` cancels the others. `SIGINT` or `SIGTERM` requests a graceful shutdown and services stop in reverse order.

## Requirements

- Python 3.11+
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
- an OpenAI-compatible embeddings endpoint

Main dependencies are listed in `requirements.txt`:

- `telethon`
- `openai`
- `python-dotenv`

## Install

```bash
git clone https://github.com/aenadgrleey/barabot.git
cd barabot
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

## Configure

Interactive setup:

```bash
.venv/bin/python -m barabot setup
.venv/bin/python -m barabot setup scraper
.venv/bin/python -m barabot run --check
```

The scraper authorization step uses Telethon and may request the phone number, confirmation code, and 2FA password. It stores the resulting session only on the local machine.

Headless configuration:

```bash
BARABOT_ENV=develop \
API_ID='<telegram-api-id>' \
API_HASH='<telegram-api-hash>' \
OPENAI_API_KEY='<embedding-api-key>' \
BOT_TOKEN='<botfather-token>' \
.venv/bin/python -m barabot setup --headless

# Telegram OTP/2FA authorization is always a separate local step.
.venv/bin/python -m barabot setup scraper
```

Private configuration files:

```text
config/.env              process settings
config/scraper.env       Telegram API_ID and API_HASH
config/scraper.session   authorized Telethon session
config/processor.env     embedding-provider settings
config/bot.token         BotFather token
```

Do not commit or print these files.

## Run

```bash
.venv/bin/python -m barabot run --check
.venv/bin/python -m barabot run
```

The current runtime is only a lifecycle smoke test; it does not yet scrape, match, or notify.

## Tests

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q barabot tests
git diff --check
```

The test suite covers configuration, setup, scraper authorization boundaries, Bot API token verification, CLI routing, and runtime lifecycle behavior.

## Next product slice

Before selecting storage or queues, define and test the smallest useful flow:

1. connect Telethon in `ScraperService`;
2. receive and normalize `NewMessage`, `MessageEdited`, and `MessageDeleted` events;
3. define which chats are in scope;
4. pass normalized events through the matching behavior;
5. deliver one result to a Telegram user;
6. introduce persistence only for state that this verified flow requires.
