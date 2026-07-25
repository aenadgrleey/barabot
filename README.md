# Barabot

Barabot — заготовка multi-tenant Telegram-бота для поиска интересующих объявлений в выбранных чатах.

Проект сейчас находится в процессе архитектурной переработки. Старый монолитный userbot удалён; вместо него строится один runtime из трёх независимых сервисов, связанных надёжными SQLite-очередями.

> **Текущий статус:** инфраструктура и очереди готовы и протестированы, но scraper, processor и bot пока являются заглушками. Запуск процесса проверяет конфигурацию и lifecycle, однако ещё не читает Telegram, не считает embeddings и не отправляет уведомления.

## Архитектура

```text
Telegram
   │
   ▼
ScraperService
   │  inbox_queue
   ▼
ProcessorService
   │  outbox_queue
   ▼
BotService
   │
   ▼
Telegram user

command_queue — входящие пользовательские команды и их replay/audit trail
```

Все сервисы работают в одном процессе под `asyncio.TaskGroup` и реализуют общий lifecycle:

```python
async def start() -> None
async def run_forever(stop: asyncio.Event) -> None
async def stop() -> None
```

Если один сервис падает, `TaskGroup` отменяет остальные. При `SIGINT` или `SIGTERM` общий stop-event запускает симметричное завершение сервисов в обратном порядке.

## Что реализовано

- единая точка входа `python -m barabot run`;
- настройка credentials scraper отдельно от OTP/2FA-авторизации через `python -m barabot setup scraper`;
- интерактивная настройка processor через `python -m barabot processor configure`;
- проверка и сохранение BotFather-токена через `python -m barabot bot auth <token>`;
- проверка конфигурации до запуска event loop;
- общий lifecycle для scraper, processor и bot;
- graceful shutdown по `SIGINT` и `SIGTERM`;
- single-instance lock и PID-файл;
- SQLite в WAL-режиме с `busy_timeout`;
- сохранение существующей таблицы `items`;
- три очереди: `inbox_queue`, `outbox_queue`, `command_queue`;
- атомарный конкурентный `claim` через `UPDATE ... RETURNING`;
- lease и повторный claim зависших задач;
- `ack` с удалением обработанных строк;
- `nack` с возвратом задачи в `pending` и накоплением текста ошибок;
- идемпотентная постановка Telegram-сообщений в inbox по `(chat_id, msg_id)`;
- тесты конкурентного доступа к очередям.

## Что ещё не реализовано

- подключение Telegram-клиента в `ScraperService`;
- производный watch-list из пользовательских списков чатов;
- вычисление дельты добавленных и удалённых чатов;
- запись Telegram-сообщений в `inbox_queue`;
- embedding и semantic matching в `ProcessorService`;
- маршрутизация результатов конкретным пользователям;
- чтение `outbox_queue` и отправка сообщений в `BotService`;
- обработка пользовательских команд;
- production-развёртывание и мониторинг.

## Структура проекта

```text
barabot/
├── __main__.py                 # CLI и runtime composer
├── core/
│   ├── config.py               # загрузка и валидация .env
│   ├── db.py                   # SQLite schema, migrations, WAL
│   ├── locks.py                # process lock и PID-файл
│   ├── queue.py                # enqueue/claim/ack/nack
│   └── service.py              # общий Service Protocol
└── domains/
    ├── scraper/service.py      # Telegram intake, пока stub
    ├── processor/service.py    # embeddings/matching, пока stub
    └── bot/
        ├── service.py          # Telegram delivery, пока stub
        ├── client.py           # временный backend Protocol
        └── model/              # модели сообщений и событий

tests/test_queue.py             # тесты трёх SQLite-очередей

config/                         # приватная локальная конфигурация и сессия (не в Git)
data/                           # локальные БД, lock и PID (не в Git)
deploy/                         # локальные deploy scripts/units (не в Git)
```

## Требования

- Python 3.11+
- SQLite 3.35+ (`RETURNING` используется при claim/enqueue)
- Telegram API credentials с [my.telegram.org](https://my.telegram.org)
- OpenAI-совместимый embeddings endpoint

Основные зависимости:

- `telethon`
- `openai`
- `python-dotenv`

## Установка

```bash
git clone git@github.com:aenadgrleey/barabot.git
cd barabot

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p config
cp .env.example config/.env
```

Для запуска тестов установи pytest в виртуальное окружение:

```bash
.venv/bin/pip install pytest
```

## Первичная настройка

Запусти единый возобновляемый мастер настройки из корня проекта:

```bash
.venv/bin/python -m barabot setup
```

Команда создаёт только отсутствующие локальные файлы, пропускает уже настроенные компоненты и сохраняет scraper credentials, но не запускает Telegram-вход. После остальных шагов она показывает незавершённые команды; OTP/2FA выполняется только отдельной командой `setup scraper`. Офлайн-проверка и сообщение `Setup complete` появляются только когда все артефакты, включая авторизованную сессию, готовы.

### Настройка без терминальных запросов

Для контейнера или CI можно независимо применить любую полную группу переменных:

```bash
BARABOT_ENV=develop \
API_ID='<telegram-api-id>' \
API_HASH='<telegram-api-hash>' \
OPENAI_API_KEY='<embedding-api-key>' \
BOT_TOKEN='<botfather-token>' \
.venv/bin/python -m barabot setup --headless
```

Группы независимы: `BARABOT_ENV`; пара `API_ID` + `API_HASH`;
`OPENAI_API_KEY` с необязательными processor-параметрами; `BOT_TOKEN`.
Полностью отсутствующая группа пропускается, а частичная отклоняется с именем
недостающей переменной. Поэтому допустимы инкрементальные запуски, например:

```bash
BARABOT_ENV=prod .venv/bin/python -m barabot setup --headless
API_ID='<id>' API_HASH='<hash>' .venv/bin/python -m barabot setup --headless
OPENAI_API_KEY='<key>' .venv/bin/python -m barabot setup --headless
```

Headless-режим никогда не запускает Telegram-вход и не изменяет
`config/scraper.session`; `--force` заменяет только явно переданные группы.
После сохранения credentials выполни OTP/2FA локально: `.venv/bin/python -m
barabot setup scraper`. Сессия, API-ключи и токены являются секретами: не
сохраняй их в репозитории и не печатай в CI-логах.

## Конфигурация

Приватная конфигурация разделена по назначению:

```text
config/.env              process-wide настройки: BARABOT_ENV, WATCH_CHATS, LOG_LEVEL и reporter
config/scraper.env       Telegram API_ID и API_HASH
config/scraper.session   авторизованная Telethon-сессия
config/processor.env     ключ и параметры embeddings
config/bot.token         BotFather-токен
```

При запуске Barabot послойно загружает `config/.env`, `config/scraper.env` и `config/processor.env`. Уже установленные переменные окружения имеют приоритет. Файлы с доменными секретами создают соответствующие команды настройки, поэтому API credentials и параметры embeddings не нужно копировать в `config/.env`.

Скопируй публичный шаблон process-wide настроек:

```bash
mkdir -p config
cp .env.example config/.env
```

`BARABOT_ENV` обязателен и принимает ровно два значения: `develop` или `prod`. В `config/.env` также остаются `WATCH_CHATS`, `LOG_LEVEL` и несекретные параметры reporter. Все файлы в `config/` являются приватным локальным состоянием и игнорируются Git.

`WATCH_CHATS` пока сохраняется как переходная настройка. Планируемая модель — таблица пользовательских чатов и дедуплицированный watch-list, вычисляемый самим scraper.

## Проверка конфигурации

```bash
.venv/bin/python -m barabot run --check
```

При успешной проверке команда печатает краткую сводку конфигурации. Она проверяет допустимость `BARABOT_ENV`, Telegram API credentials, параметры embeddings, log level и числовые ограничения.

Если настройка не завершена, команда завершается с ошибкой и печатает подходящие команды для отсутствующих файлов, например:

```text
missing setup:
  .venv/bin/python -m barabot setup
  .venv/bin/python -m barabot setup scraper
  .venv/bin/python -m barabot processor configure
  .venv/bin/python -m barabot bot auth <token>
```

Проверка полностью офлайн: `run --check` только загружает слои конфигурации и проверяет локальные значения. Она не создаёт Telegram-клиент и не обращается к сети.

## Вход в Telegram для scraper

Выполни одну команду:

```bash
.venv/bin/python -m barabot setup scraper
```

Для этой команды не нужна готовая process-wide конфигурация. Она использует сохранённый `config/scraper.env`; если credentials отсутствуют или некорректны, команда запросит `Telegram API_ID` и скрыто запросит `Telegram API_HASH`, проверит их формат и сохранит файл с правами `0600`. Затем Telethon запросит номер телефона, код подтверждения и, если включена двухэтапная аутентификация, пароль.

Успешный вход создаёт два приватных файла:

- `config/scraper.env` — `API_ID` и `API_HASH`;
- `config/scraper.session` — постоянная авторизованная Telethon-сессия.

При повторном запуске команда использует сохранённые credentials. Сессию и credentials нельзя публиковать или добавлять в Git.

## Настройка processor

```bash
.venv/bin/python -m barabot processor configure
```

Команда не загружает process-wide `Config`. Она скрыто запрашивает обязательный `OPENAI_API_KEY`, а затем предлагает значения для остальных параметров; Enter принимает значение в квадратных скобках:

```text
EMBEDDING_MODEL [text-embedding-3-small]
EMBEDDING_URL [https://api.openai.com/v1/embeddings]
SIMILARITY_THRESHOLD [0.85]
EMBEDDING_CONCURRENCY [4]
EMBEDDING_RETRIES [3]
EMBEDDING_TIMEOUT [30]
```

После валидации команда атомарно записывает `config/processor.env` с правами `0600`. Повторный запуск заменяет этот файл целиком.

## Авторизация Telegram-бота

Получи токен у BotFather и передай его обязательным аргументом:

```bash
.venv/bin/python -m barabot bot auth <token>
```

Команда проверяет токен методом Bot API `getMe`, затем атомарно записывает только его исходное значение в `config/bot.token` с правами `0600`. При ошибке проверки существующий файл токена остаётся без изменений. Токен не выводится; команда показывает только id/username бота и путь к файлу.

## Запуск

```bash
.venv/bin/python -m barabot run
```

На текущем этапе процесс поднимет три lifecycle-заглушки и будет ждать сигнала остановки. Это smoke test runtime, а не функциональный запуск бота.

Остановка:

```bash
kill -TERM "$(cat data/barabot.pid)"
```

Повторный `SIGINT`/`SIGTERM` используется как принудительный выход, если graceful shutdown завис.

## Тесты

```bash
.venv/bin/python -m pytest -q
```

Проверяются:

- enqueue для всех трёх очередей;
- inbox-дедупликация;
- FIFO claim;
- лимит пачки;
- конкурентный claim без double-claim;
- lease expiration и reclaim;
- `ack` и `nack`;
- накопление ошибок;
- совместимость schema migration с существующей БД.

Дополнительные локальные проверки:

```bash
.venv/bin/python -m compileall -q barabot tests
git diff --check
```

## SQLite

По умолчанию данные хранятся в:

```text
data/barabot.sqlite
```

Таблицы:

- `items` — сообщения и embeddings из предыдущей реализации;
- `inbox_queue` — сырые Telegram-сообщения для processor;
- `outbox_queue` — готовые уведомления для bot;
- `command_queue` — пользовательские команды.

Состояния очереди:

```text
pending → processing → ack/delete
               │
               ├─ nack → pending
               └─ expired lease → повторный claim
```

Обработанная строка удаляется через `ack`, а не хранится со статусом `done`. Это ограничивает бесконтрольный рост очередей. Для аудита команд понадобится отдельная политика хранения до подключения реального command processor.

Быстрая проверка базы:

```bash
sqlite3 data/barabot.sqlite 'PRAGMA integrity_check;'
sqlite3 data/barabot.sqlite \
  "SELECT 'inbox', count(*) FROM inbox_queue
   UNION ALL SELECT 'outbox', count(*) FROM outbox_queue
   UNION ALL SELECT 'commands', count(*) FROM command_queue;"
```

## Развёртывание

Каталог `deploy/` является локальным и игнорируется Git вместе с `config/` и `data/`. Локальные scripts/units должны вызывать актуальный CLI: сначала `python -m barabot run --check`, затем `python -m barabot run`. Сервис пока не следует включать как production-бот: доменные сервисы ещё являются заглушками.

## Ближайший этап

Следующий вертикальный срез:

1. добавить таблицы `user_chats` и `user_prefs`;
2. реализовать CRUD и тесты;
3. получать активные чаты через `SELECT DISTINCT chat_id`;
4. вычислять `added = target - current` и `removed = current - target`;
5. подключить Telethon в `ScraperService`;
6. класть новые сообщения в `inbox_queue`;
7. проверить путь Telegram event → SQLite интеграционным тестом.

После этого можно последовательно оживлять `ProcessorService`, а затем `BotService`.

## Безопасность репозитория

Репозиторий содержит публичный код и безопасный шаблон `.env.example`. Приватное и машинно-зависимое состояние остаётся только локально в игнорируемых каталогах `config/`, `data/` и `deploy/`.

Не коммитить:

- `config/.env`, `config/scraper.env`, `config/scraper.session`, `config/processor.env` и `config/bot.token`;
- API keys;
- `data/*.sqlite`, WAL/SHM и lock-файлы;
- локальные deploy scripts/units;
- `.venv/`;
- `__pycache__/` и pytest cache.

Все перечисленные пути должны оставаться в `.gitignore`.
