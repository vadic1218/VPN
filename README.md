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
- `/cancel`
- `/add HH:MM текст`
- `/list`
- `/off ID`
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

## Автоматическая проверка и выкладка

- В `.github/workflows/ci.yml` добавлена проверка синтаксиса и тестов на каждый `push` и `pull request`.
- Если Railway подключён к GitHub-репозиторию, то после успешного `git push` в основную ветку он автоматически подхватит новый коммит и перезальёт бота на сервер.
- Для полностью автоматической выкладки со стороны Codex всё равно нужны настроенные доступы к GitHub и серверу. Без них я могу подготовить проект, но не выполнять будущие пуши самовольно.

### Локальный безопасный auto-publish

- Скрипт `scripts/auto_publish.ps1` запускает проверку синтаксиса, тесты, делает `git add -A`, создаёт коммит и пушит в `origin/main`.
- Скрипт блокирует публикацию секретов и локальной базы: `.env`, `config.json`, `*.db`, `*.sqlite`, `*.sqlite3`.
- Если локальная ветка `main` расходится с `origin/main`, публикация останавливается до ручной синхронизации.

Запуск:

```powershell
.\auto_publish.bat
```

Своё сообщение коммита:

```powershell
.\auto_publish.bat -CommitMessage "feat: update reminder flow"
```

## Важно

- Не коммить токен в GitHub.
- Если токен уже где-то светился, лучше перевыпустить его через BotFather.
- Без Volume файл `reminders.db` в Railway может потеряться при пересоздании контейнера.
