# Telegram bot reminder

Простой Telegram-бот, который каждый день напоминает выпить таблетки.

## Что умеет

- Добавлять ежедневные напоминания через Telegram.
- Показывать список активных напоминаний.
- Удалять ненужные напоминания.
- Хранить расписание в SQLite.

## Команды в Telegram

- `/start`
- `/help`
- `/add HH:MM текст`
- `/list`
- `/delete ID`

Пример:

```text
/add 09:00 Выпить таблетки после завтрака
```

## Локальный запуск

1. Создай `config.json` по образцу `config.example.json`
   или задай переменные окружения из `.env.example`.
2. Запусти:

```powershell
python bot.py
```

Для Windows также есть `start_bot.bat`.

## Railway + GitHub autodeploy

В проект уже добавлен `railway.json`, поэтому Railway будет запускать бота командой:

```text
python bot.py
```

Что нужно сделать в Railway:

1. Залить проект в GitHub.
2. В Railway создать новый проект.
3. Выбрать `Deploy from GitHub repo`.
4. Подключить этот репозиторий.
5. В Variables добавить:

```text
TELEGRAM_BOT_TOKEN=твой_токен_бота
BOT_TIMEZONE=Europe/Volgograd
DB_PATH=/data/reminders.db
```

6. Если хочешь, чтобы база не терялась между деплоями, подключи Volume и используй `DB_PATH=/data/reminders.db`.

## Важно

- Не коммить токен в GitHub.
- Если токен уже где-то светился, лучше перевыпустить его через BotFather.
- Без Volume файл `reminders.db` в Railway может потеряться при пересоздании контейнера.
