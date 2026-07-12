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
deploy/barabot.service          # sample systemd unit
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
cp .env.example .env
```

Для запуска тестов установи pytest в виртуальное окружение:

```bash
.venv/bin/pip install pytest
```

## Конфигурация

Barabot читает `.env` из корня проекта.

Обязательные переменные:

```dotenv
API_ID=123456
API_HASH=0123456789abcdef0123456789abcdef
SESSION_STRING=...
OPENAI_API_KEY=...
```

Опциональные переменные и значения по умолчанию:

```dotenv
WATCH_CHATS=-1001234567890
SIMILARITY_THRESHOLD=0.85
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_URL=https://api.openai.com/v1/embeddings
EMBEDDING_CONCURRENCY=4
EMBEDDING_RETRIES=3
EMBEDDING_TIMEOUT=30
LOG_LEVEL=INFO
```

`.env` игнорируется Git и не должен попадать в коммиты.

`WATCH_CHATS` пока сохраняется как переходная настройка. Планируемая модель — таблица пользовательских чатов и дедуплицированный watch-list, вычисляемый самим scraper.

## Проверка конфигурации

```bash
.venv/bin/python -m barabot run --check
```

Пример:

```text
barabot config ok: api_id=123456 chats=2 threshold=0.85 log_level=INFO db=/path/to/barabot/data/barabot.sqlite
```

Команда проверяет:

- наличие обязательных переменных;
- формат `API_HASH`;
- диапазон similarity threshold;
- log level;
- параметры concurrency, retries и timeout.

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

## systemd

В `deploy/barabot.service` лежит пример unit-файла. Перед установкой обязательно проверь:

- `User=`;
- `WorkingDirectory=`;
- пути к `.venv`;
- `ReadWritePaths=`;
- команды запуска: актуальный CLI требует подкоманду `run`.

Рекомендуемые команды внутри unit:

```ini
ExecStartPre=/path/to/barabot/.venv/bin/python -m barabot run --check
ExecStart=/path/to/barabot/.venv/bin/python -m barabot run
```

После адаптации:

```bash
sudo cp deploy/barabot.service /etc/systemd/system/barabot.service
sudo systemctl daemon-reload
sudo systemctl enable --now barabot.service
systemctl status barabot.service
journalctl -u barabot.service -f
```

Сервис пока не следует включать как production-бот: доменные сервисы ещё являются заглушками.

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

Не коммитить:

- `.env` и Telegram session string;
- API keys;
- `data/*.sqlite`, WAL/SHM и lock-файлы;
- `.venv/`;
- `__pycache__/` и pytest cache.

Все перечисленные пути должны оставаться в `.gitignore`.
