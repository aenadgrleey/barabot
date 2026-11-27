# Barabot

Barabot is a Python service for finding relevant listings in selected Telegram chats.

## Architecture

Barabot runs in one process: a scraper and a bot service on one asyncio loop, with background processing in [DBOS](https://docs.dbos.dev) workflows. The scraper owns the Telegram user connection and writes messages to `data/barabot.sqlite`. Each event schedules a debounced `process_listing` run that assembles the listing, saves a new version and checks it against the subscriptions of its chat with [TypeSafe Jev](https://docs.typesafe.ai). Jev first reads the text; if a subscription comes back unclear and the listing has photos, it asks again with them. Decisions go to the `matches` table. For every user who must hear about a version, a `notify` workflow sends the bot message once and edits it later as the listing changes, no longer matches or is deleted; `deliveries` records what each user sees. DBOS keeps workflow state in `data/dbos.sqlite`. The processor domain owns the TypeSafe API key. The bot domain owns the BotFather token. The scraper session and bot token belong to separate Telegram identities. The full design, including what is not built yet, is in [docs/architecture.html](docs/architecture.html).

At startup, Barabot loads the configuration, takes an exclusive lock on `data/barabot.lock` and writes its PID to `data/barabot.pid`. It migrates the app database, launches DBOS, then starts the scraper and bot and runs them in one `asyncio.TaskGroup`. If one task fails, the group cancels the others. On `SIGINT` or `SIGTERM`, the services stop in reverse order and DBOS shuts down last. See the [entry point](barabot/__main__.py) and [service contract](barabot/core/service.py).

## Run locally

Dependencies are declared in [pyproject.toml](pyproject.toml) and pinned in `uv.lock`. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then, from the repository root, create `.venv` for the Telegram connection of this installation. It needs Python 3.11 or newer; uv uses one it finds or downloads it.

| Connection | Install | Session owner |
| --- | --- | --- |
| Shared gateway | `uv sync --extra tgmux` | The [tgmux gateway](https://github.com/aenadgrleey/tgmux) holds the Telegram session. Barabot connects to its Unix socket as integration `barabot` on the default instance. |
| Direct Telethon | `uv sync` | Barabot opens its own session in `config/scraper.session`. |

`uv sync` also installs the dev tools. A later plain `uv sync` removes tgmux again, so repeat `--extra tgmux` when you use the gateway. Run the commands below with `source .venv/bin/activate` first, or prefix them with `uv run`.

For the gateway, run `python -m barabot setup` under the same OS user as tgmux. For a direct connection, run `TELEGRAM_BACKEND=telethon python -m barabot setup` on first setup, then `python -m barabot setup scraper` to authorize the local session with your phone number, login code and 2FA if enabled. Setup also asks for the `API_ID` and `API_HASH` of a Telegram application from [my.telegram.org](https://my.telegram.org), which the bot needs with either connection, a TypeSafe API key and a BotFather token.

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
| `config/telegram.env` | `API_ID` and `API_HASH` of the Telegram application from [my.telegram.org](https://my.telegram.org). Required: the bot connects with them, and a local scraper session uses them too. An older `scraper.env` is renamed to it on start |
| `config/scraper.session` | Authorized local Telethon session |
| `config/processor.env` | `TYPESAFE_API_KEY` for subscription matching |
| `config/mcp.env` | MCP connector: `MCP_PUBLIC_URL`, `MCP_PORT`, `TELEGRAM_LOGIN_CLIENT_ID`, `TELEGRAM_LOGIN_CLIENT_SECRET`. Required |
| `config/bot.token` | Verified BotFather token |
| `config/bot.session` | The bot's Telethon session, created on first start |

To replace the TypeSafe key, run `python -m barabot processor configure`. For unattended setup, supply `BARABOT_ENV`, `TELEGRAM_BACKEND`, `TYPESAFE_API_KEY` and `BOT_TOKEN` in the environment and run `python -m barabot setup --headless`. For direct Telethon, also supply `API_ID` and `API_HASH`, then authorize the session interactively once. See the [environment template](.env.example).

## Subscriptions

A subscription has three parts, all in plain words. `author` is what the author of a wanted message does, `query` is what the message is about, and the optional `author-not` names the role that must not match. Without it Jev confuses roles that share a topic, such as landlord and tenant. A subscription only sees the chats it lists. Until the bot manages subscriptions, use the CLI:

```bash
python -m barabot subscription add --user 123456789 --chat -1001234567890 \
  --author "сдаёт жильё" --author-not "автор ищет жильё, продаёт что-то или пишет о другом" \
  --query "квартира от 2 комнат, до 20 000 Kč в месяц"
python -m barabot subscription list
python -m barabot subscription remove 1
```

A listing matches when Jev gives the author role at least 0.6 and answers `fits` for the query. Every check, matched or not, is stored in `matches` with the probabilities, so you can see why a listing was or was not sent.

## MCP connector

Users can manage subscriptions from their own agent. Barabot serves a remote MCP server at `<MCP_PUBLIC_URL>/mcp` with four tools (list, add, update, remove) over `BarabotApi`. The user adds that URL as a connector in Claude or another MCP client with OAuth and logs in with Telegram once. Barabot is the OAuth server for the client; Telegram's [OpenID Connect login](https://core.telegram.org/bots/telegram-login) tells it who the user is. The login asks for `telegram:bot_access`, so the bot can send listings without `/start`, and the bot greets the user right after the login. The code is in [barabot/domains/mcp](barabot/domains/mcp).

Setup:

1. In @BotFather, open the bot, then **Login Widget**. Copy the Client ID and Client Secret, and add `<MCP_PUBLIC_URL>/telegram/callback` to Allowed URLs.
2. Publish the local port over HTTPS on port 443. Claude connects from Anthropic's servers and [cannot use a URL that names a port](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp), so barabot refuses an `MCP_PUBLIC_URL` with one. On the server, with Tailscale Funnel: `tailscale funnel --bg --https=443 http://127.0.0.1:8765`, which makes `MCP_PUBLIC_URL=https://aenaut.ilish-leaffish.ts.net`.
3. Write `config/mcp.env`:

```bash
MCP_PUBLIC_URL=https://aenaut.ilish-leaffish.ts.net
MCP_PORT=8765
TELEGRAM_LOGIN_CLIENT_ID=...
TELEGRAM_LOGIN_CLIENT_SECRET=...
```

Barabot does not start without this file; `run --check` prints `mcp=<url>`. OAuth clients, codes and tokens live in the `oauth_*` tables; tokens are stored as hashes. Access tokens last an hour, refresh tokens 90 days and rotate on use.

## Deploy

The server runs barabot next to the tgmux gateway, with the same branch scheme as tgmux. `dev` is where work lands, `prod` is what the server runs. A push to `prod` deploys itself within about a minute:

```bash
git push origin dev:prod     # fast-forward only: prod never gets ahead of dev
```

`barabot-deploy.timer` runs [packaging/deploy.sh](packaging/deploy.sh) `--auto` every minute. When `origin/prod` moves, it checks out the commit, runs `uv sync --locked --extra tgmux --no-dev` into `/opt/barabot/.venv`, updates the systemd units from [packaging/systemd](packaging/systemd), runs `python -m barabot run --check` and restarts `barabot.service`. If the check fails or the service does not stay up for 10 seconds, it restores the previous commit. It logs the result to journald as `barabot-deploy`: success at `notice`, failure and rollback at `err`. On aenaut, aenaut-notify forwards both to Telegram. A failed commit is not retried until `prod` moves again, and the timer never starts a service you stopped. By hand: `barabot-deploy`, `barabot-deploy --dry-run`, `barabot-deploy REF`.

First install on the server, as root. uv goes to `/usr/local/bin`, where the deploy timer finds it. The venv must use the system `python3`, because `barabot.service` cannot see a Python that uv downloads into `/root`.

```bash
curl -LsSf https://astral.sh/uv/0.12.13/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
git clone -b prod https://github.com/aenadgrleey/barabot.git /opt/barabot
cd /opt/barabot && UV_NO_MANAGED_PYTHON=1 uv sync --locked --extra tgmux --no-dev --compile-bytecode
ln -s /opt/barabot/packaging/deploy.sh /usr/local/bin/barabot-deploy
ln -s /opt/barabot/packaging/deploy.sh /etc/aenaut-deploy/projects/barabot

# config/ is not in git: .env (BARABOT_ENV=prod, TELEGRAM_BACKEND=tgmux), telegram.env, processor.env, bot.token
mkdir -p -m 700 /opt/barabot/config /opt/barabot/data
tgmux integration add barabot        # if the gateway does not register on first connect
cd /opt/barabot && .venv/bin/python -m barabot run --check

cp /opt/barabot/packaging/systemd/barabot{,-deploy}.service /opt/barabot/packaging/systemd/barabot-deploy.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now barabot barabot-deploy.timer
journalctl -u barabot -f
```

A server installed before the move to uv runs the old deploy script, which looks for `requirements-tgmux.txt`, so the first `uv.lock` commit is deployed once by hand with the new script. Install uv as above, then:

```bash
systemctl stop barabot-deploy.timer             # before the push, or the old script fails and rolls back
git push origin dev:prod                        # from your machine
git -C /opt/barabot fetch origin
git -C /opt/barabot show origin/prod:packaging/deploy.sh > /root/barabot-deploy-uv.sh
bash /root/barabot-deploy-uv.sh && rm /root/barabot-deploy-uv.sh
systemctl start barabot-deploy.timer
```

`uv sync` updates the existing `.venv` to match `uv.lock`. If the timer got there first, it reports a failed deploy and rolls back; the same steps then finish the move. After that, the timer runs the new script.

`barabot.service` hides `/root` except `/root/.local/state/tgmux/default`, where the gateway socket and the integration lock live.

## Tests

```bash
uv run pytest -q
uv run ruff check
uv run ruff format --check
uv run pyright
```

The test run takes about a minute, mostly DBOS. Ruff uses its default rule set with the exceptions in [pyproject.toml](pyproject.toml); `uv run ruff check --fix` and `uv run ruff format` apply its fixes. Pyright reads library code where a library has no type hints, which is how it checks calls into Telethon.
