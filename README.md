# bara_bot

Userbot, который слушает Telegram-чаты, эмбеддит сообщения и **пишет в лог**,
когда в потоке появляется что-то **семантически новое** (cosine similarity
ниже порога). Без лички, без пушей — только stdout и SQLite.

## Слоп-фичи

- Подписка на список чатов через userbot (Pyrogram, MTProto).
- Новые сообщения и **редактирования** идут в общий пайплайн.
- Эмбеддинг через OpenAI-совместимый `/v1/embeddings` (по умолчанию OpenAI).
- Линейный скан по SQLite, без очередей и воркеров.
- WAL-режим, асинхронный слой поверх sync sqlite3, `BEGIN IMMEDIATE` на запись.
- Concurrency-семафор и retry с exp-backoff для эмбеддинга.
- Single-instance lock (`flock`), PID-файл, корректный SIGTERM.
- `--check` для валидации конфига, sample systemd unit в `deploy/`.
- Новые ивенты: `log.info("NEW: …")` + строка в `data/barabot.sqlite`.
- Дубликаты: `log.info("dup: …")`, в базу не пишутся.

## Установка

```bash
cd ~/projects/barabot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Настройка

1. Получить `API_ID` и `API_HASH` на https://my.telegram.org
2. Сминтить сессию (один раз):
   ```bash
   python -m barabot.session_gen
   ```
   Скопировать распечатанный `SESSION_STRING=...` в `.env`.
3. Заполнить `WATCH_CHATS` (id через запятую; `-100…` для групп/каналов).
4. Указать `OPENAI_API_KEY` или другой `EMBEDDING_URL`.
5. Проверить конфиг:
   ```bash
   python -m barabot --check
   ```

## Запуск

```bash
python -m barabot
```

Логи идут в stdout. Для долгого прогона:

```bash
python -m barabot 2>&1 | tee -a data/barabot.log
```

### Под systemd

Скопировать `deploy/barabot.service` в `/etc/systemd/system/`,
поправить `User=`, `WorkingDirectory=`, `ReadWritePaths=`:

```bash
sudo cp deploy/barabot.service /etc/systemd/system/barabot.service
sudo systemctl daemon-reload
sudo systemctl enable --now barabot.service
systemctl status barabot
journalctl -u barabot -f
```

`ExecStartPre=python -m barabot --check` гарантирует, что systemd
не запустит бота с битым `.env`.

## Что увидишь

```
2026-06-23 14:02:11 INFO barabot: barabot up: watching 2 chat(s), threshold=0.85, db=.../barabot.sqlite, pid=12345
2026-06-23 14:03:42 INFO barabot: NEW: chat=-1001234567890 chat_title='Барахолка Тель-Авив' msg=4567 score=0.42 text='продам горный велосипед, 1500 шек, Хайфа'
2026-06-23 14:03:55 INFO barabot: dup: chat=-1001234567890 msg=4568 score=0.91 (matches msg 4567)
2026-06-23 14:04:01 INFO barabot: received SIGTERM, shutting down
2026-06-23 14:04:03 INFO barabot: barabot stopped cleanly
```

## Reporter bot (опционально)

Userbot пишет только в лог и SQLite. Если хочешь получать алерты **в Telegram**,
подними второй процесс `reporter`. Он читает ту же SQLite, **не пишет в неё**,
и при появлении новой строки шлёт форматированное сообщение в твой(и) чат(ы).

### Что умеет

- `/start`, `/help` — приветствие
- `/latest [N]` — последние N (default 10, max 100)
- `/chats` — счётчики по чатам
- `/search <текст>` — substring-поиск
- `/get <chat_id> <msg_id>` — полный текст одного item
- Авто-пуш: каждое новое «🆕 NEW» в момент записи

### Настройка

1. Создать бота через [@BotFather](https://t.me/BotFather), получить токен.
2. Добавить в `.env`:
   ```
   REPORTER_BOT_TOKEN=...   # bot token from @BotFather
   ALLOWED_CHAT_IDS=330216451   # your user id; comma-separated for multiple
   REPORTER_POLL_INTERVAL=2.0
   REPORTER_DEFAULT_LIMIT=10
   ```
3. Написать боту `/start` — он зарегистрирует чат в логах (если id уже в `ALLOWED_CHAT_IDS`).
4. Запустить:
   ```bash
   python -m barabot reporter --check   # валидация
   python -m barabot reporter           # запуск
   ```

### Под systemd

```bash
sudo cp deploy/barabot-reporter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now barabot-reporter.service
```

Reporter зависит от `barabot.service` (см. `After=`), стартует **после** userbot.

### Безопасность

- `ALLOWED_CHAT_IDS` — **whitelist**. Пустой список ⇒ бот не отвечает ни на какие команды.
- Reporter открывает БД **read-only** (`mode=ro` через URI). Не может ничего записать в barabot'овские embeddings/метаданные.
- `flock` отдельный (`barabot-reporter.lock`), reporter и userbot не блокируют друг друга.

## Что лежит в базе

```bash
sqlite3 ~/projects/barabot/data/barabot.sqlite \
  'SELECT chat_id, msg_id, sender, substr(text, 1, 80), created_at FROM items ORDER BY created_at DESC LIMIT 20;'
```

## Тест без сети

```bash
.venv/bin/python smoke.py
```

Покрывает: валидацию конфига, WAL roundtrip, async-обёртки db, cosine,
retry на 5xx (с реальным локальным HTTP-сервером), fail-fast на 4xx (через
мок), single-instance lock через `flock`.

## Что НЕ умеет (намеренно)

- Нет очередей — обработка последовательная, как просили.
- Нет загрузки истории — только то, что приходит после старта.
- Нет алертов в личку, нет бота-командера — это наблюдатель.
- Нет веб-интерфейса, тюнинга порогов на лету, классификации.
- Нет multi-tenant.
