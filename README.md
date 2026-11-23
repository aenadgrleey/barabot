# Barabot

Barabot is a Python service for finding relevant listings in selected Telegram chats.

## Architecture

Barabot runs three services in one process. The scraper owns the Telegram user connection. The processor domain owns the embedding model, provider settings and similarity threshold. The bot domain owns the BotFather token for the user-facing bot. The scraper session and bot token belong to separate Telegram identities.

At startup, Barabot loads the configuration, takes an exclusive lock on `data/barabot.lock` and writes its PID to `data/barabot.pid`. It starts the scraper, processor and bot in that order, then runs their tasks in one `asyncio.TaskGroup`. If one task fails, the group cancels the others. On `SIGINT` or `SIGTERM`, the services stop in reverse order and the scraper closes its connection last. See the [entry point](barabot/__main__.py) and [service contract](barabot/core/service.py).

## Run locally

Use Python 3.11 or newer. From the repository root, create the virtual environment and choose the Telegram connection for this installation.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

| Connection | Install | Session owner |
| --- | --- | --- |
| Shared gateway | `pip install -r requirements-tgmux.txt` | The [tgmux gateway](https://github.com/aenadgrleey/tgmux) holds the Telegram session. Barabot connects to its Unix socket as integration `barabot` on the default instance. |
| Direct Telethon | `pip install -r requirements.txt` | Barabot opens its own session in `config/scraper.session`. Get the application credentials from [my.telegram.org](https://my.telegram.org). |

For the gateway, run `python -m barabot setup` under the same OS user as tgmux. For a direct connection, run `TELEGRAM_BACKEND=telethon python -m barabot setup` on first setup, then `python -m barabot setup scraper` to authorize the local session with your phone number, login code and 2FA if enabled. Setup also asks for an embeddings API key and a BotFather token.

```bash
python -m barabot run --check
python -m barabot run
```

`run --check` validates the local configuration offline. Look for `scraper=tgmux` or `scraper=telethon` in its output. The subsequent `run` opens the connection.

## Configuration

Set `TELEGRAM_BACKEND` in `config/.env`. An exported environment variable takes precedence. The default, `tgmux`, falls back to the local Telethon session if Python cannot import `tgmux.SharedClient`. Once the gateway client loads, it retries the socket connection when the gateway is down. Set `telethon` to use the local session directly.

| Local file | Contents |
| --- | --- |
| `config/.env` | Deployment mode (`BARABOT_ENV=develop` or `prod`), Telegram backend, chat IDs and log level |
| `config/scraper.env` | `API_ID` and `API_HASH` for direct Telethon |
| `config/scraper.session` | Authorized local Telethon session |
| `config/processor.env` | Embeddings API key, endpoint, model and similarity settings |
| `config/bot.token` | Verified BotFather token |

To change the embedding provider, run `python -m barabot processor configure`. For unattended setup, supply `BARABOT_ENV`, `TELEGRAM_BACKEND`, `OPENAI_API_KEY` and `BOT_TOKEN` in the environment and run `python -m barabot setup --headless`. For direct Telethon, also supply `API_ID` and `API_HASH`, then authorize the session interactively once. See the [environment template](.env.example).

## Deploy with tgmux

Run these commands as root from the configured production checkout with `barabot-prod.service` installed. The systemd drop-in gives the service access to the default gateway socket and the directory where tgmux creates its integration lock. If the gateway requires manual registration, run `tgmux integration add barabot` once.

```bash
.venv/bin/pip install -r requirements-tgmux.txt
install -Dm644 packaging/systemd/tgmux.conf /etc/systemd/system/barabot-prod.service.d/tgmux.conf
systemctl daemon-reload
.venv/bin/python -m barabot run --check
systemctl restart barabot-prod.service
journalctl -u barabot-prod.service -f
```

## Tests

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
```
