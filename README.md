# Barabot

Barabot is a Python service for finding relevant listings in selected Telegram chats.

## Architecture

Barabot runs in one process: a scraper and a bot service on one asyncio loop, with background processing in [DBOS](https://docs.dbos.dev) workflows. The scraper owns the Telegram user connection and writes messages to `data/barabot.sqlite`. Each event schedules a debounced `process_listing` run that assembles the listing and saves a new version. DBOS keeps workflow state in `data/dbos.sqlite`. The processor domain owns the embedding settings. The bot domain owns the BotFather token. The scraper session and bot token belong to separate Telegram identities. The full design, including what is not built yet, is in [docs/architecture.html](docs/architecture.html).

At startup, Barabot loads the configuration, takes an exclusive lock on `data/barabot.lock` and writes its PID to `data/barabot.pid`. It migrates the app database, launches DBOS, then starts the scraper and bot and runs them in one `asyncio.TaskGroup`. If one task fails, the group cancels the others. On `SIGINT` or `SIGTERM`, the services stop in reverse order and DBOS shuts down last. See the [entry point](barabot/__main__.py) and [service contract](barabot/core/service.py).

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
| `config/.env` | Deployment mode (`BARABOT_ENV=develop` or `prod`), Telegram backend, chat IDs, backfill depth and log level |
| `config/scraper.env` | `API_ID` and `API_HASH` for direct Telethon |
| `config/scraper.session` | Authorized local Telethon session |
| `config/processor.env` | Embeddings API key, endpoint, model and similarity settings |
| `config/bot.token` | Verified BotFather token |

To change the embedding provider, run `python -m barabot processor configure`. For unattended setup, supply `BARABOT_ENV`, `TELEGRAM_BACKEND`, `OPENAI_API_KEY` and `BOT_TOKEN` in the environment and run `python -m barabot setup --headless`. For direct Telethon, also supply `API_ID` and `API_HASH`, then authorize the session interactively once. See the [environment template](.env.example).

## Deploy

The server runs barabot next to the tgmux gateway, with the same branch scheme as tgmux. `dev` is where work lands, `prod` is what the server runs. A push to `prod` deploys itself within about a minute:

```bash
git push origin dev:prod     # fast-forward only: prod never gets ahead of dev
```

`barabot-deploy.timer` runs [packaging/deploy.sh](packaging/deploy.sh) `--auto` every minute. When `origin/prod` moves, it checks out the commit, installs `requirements-tgmux.txt`, updates the systemd units from [packaging/systemd](packaging/systemd), runs `python -m barabot run --check` and restarts `barabot.service`. If the check fails or the service does not stay up for 10 seconds, it restores the previous commit. It logs the result to journald as `barabot-deploy`: success at `notice`, failure and rollback at `err`. On aenaut, aenaut-notify forwards both to Telegram. A failed commit is not retried until `prod` moves again, and the timer never starts a service you stopped. By hand: `barabot-deploy`, `barabot-deploy --dry-run`, `barabot-deploy REF`.

First install on the server, as root:

```bash
git clone -b prod https://github.com/aenadgrleey/barabot.git /opt/barabot
python3 -m venv /opt/barabot/.venv
/opt/barabot/.venv/bin/pip install -r /opt/barabot/requirements-tgmux.txt
ln -s /opt/barabot/packaging/deploy.sh /usr/local/bin/barabot-deploy
ln -s /opt/barabot/packaging/deploy.sh /etc/aenaut-deploy/projects/barabot

# config/ is not in git: .env (BARABOT_ENV=prod, TELEGRAM_BACKEND=tgmux), processor.env, bot.token
mkdir -p -m 700 /opt/barabot/config /opt/barabot/data
tgmux integration add barabot        # if the gateway does not register on first connect
cd /opt/barabot && .venv/bin/python -m barabot run --check

cp /opt/barabot/packaging/systemd/barabot{,-deploy}.service /opt/barabot/packaging/systemd/barabot-deploy.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now barabot barabot-deploy.timer
journalctl -u barabot -f
```

`barabot.service` hides `/root` except `/root/.local/state/tgmux/default`, where the gateway socket and the integration lock live.

## Tests

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
```
