# Развёртывание на Railway

Вся инфраструктура описана в [`.railway/railway.ts`](../.railway/railway.ts) (Railway Infrastructure as
Code). Секреты в файл не пишутся: он ссылается на shared variables проекта.

## Состав

- `Postgres` — PostgreSQL 17, как в compose и CI; `pg_dump` в образе приложения тоже 17.
- `rabbitmq` — RabbitMQ 4 с томом `rabbitmq-data`.
- `web` — Gunicorn и админка, публичный HTTPS-домен, healthcheck `/health/`. Перед каждым деплоем
  выполняется `deploy/init.sh`: миграции, начальный каталог, роли, `check --deploy`.
- `worker` — Celery worker, scheduler, приём и отправка Telegram в одном контейнере
  (`deploy/railway-worker.sh`) с томом `worker-data` в `/data`. Railway подключает том только к одному
  сервису, а отправка читает erasure guard, который пишет worker. Упавший процесс перезапускается
  внутри контейнера через 15 секунд.

Отличия от Debian: nginx нет, статика запечена в образ и отдаётся Django; админка доступна из интернета,
а не из VPN; worker работает от root (`RAILWAY_RUN_UID=0`), потому что Railway монтирует тома от root.

## Первый запуск

Нужны Railway CLI 5.42.1+ и Node.js 22+.

1. В корне проекта установите SDK для `.railway/railway.ts`, войдите и создайте проект:

   ```bash
   npm install
   railway login
   railway init --name ai-digest
   ```

2. В дашборде откройте **Project Settings → Shared Variables** и задайте переменные **до** шага 3:
   RabbitMQ запоминает пароль при первом запуске тома.

   | Переменная | Значение |
   |---|---|
   | `DJANGO_SECRET_KEY` | `openssl rand -hex 32` |
   | `ERASURE_HASH_KEY` | отдельный `openssl rand -hex 32` |
   | `RABBITMQ_PASSWORD` | `openssl rand -hex 24` (только hex: пароль входит в URL брокера) |
   | `SOURCE_HTTP_USER_AGENT` | `OwnedDigestBot/1.0 (+mailto:реальный-контакт@example.com)` |
   | `OPENROUTER_API_KEY` | ключ OpenRouter |
   | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` | задаются только парой |
   | `TELEGRAM_SOURCE_API_ID`, `TELEGRAM_SOURCE_API_HASH`, `TELEGRAM_SOURCE_PHONE` | Telethon; без сбора из Telegram оставьте пустыми |
   | `BACKUP_AGE_RECIPIENT` | публичный ключ из `age-keygen`; приватный ключ храните вне Railway |

3. Создайте сервисы, тома и переменные, затем домен для `web`:

   ```bash
   railway config plan
   railway config apply
   railway domain --service web
   ```

4. Загрузите код в оба сервиса приложения:

   ```bash
   railway up --service web
   railway up --service worker
   ```

5. Создайте администратора и, если нужен сбор из Telegram, авторизуйте Telethon-сессию (код придёт в
   Telegram, сессия сохранится на томе worker):

   ```bash
   railway ssh --service web python manage.py createsuperuser
   railway ssh --service worker python manage.py init_telethon_session
   ```

6. Проверьте `https://<домен>/health/` — ответ `{"status": "ok", ...}` — и логи:
   `railway logs --service worker`.

## Обновление

Повторите `railway up --service web` и `railway up --service worker`. Изменения инфраструктуры —
правка `.railway/railway.ts`, затем `railway config plan` и `railway config apply`. После подключения
репозитория GitHub (`railway service source connect`) деплой будет идти автоматически.

## Ограничения

- При деплое worker бот недоступен около двух минут: сервис с томом сначала останавливается. Процессам
  даётся 120 секунд на завершение; прерванные задания повторяются после истечения их lease.
- Резервные копии и erasure guard лежат на томе worker внутри того же проекта Railway. Это не внешнее
  хранилище: регулярно копируйте `/data/backups` за пределы Railway.
- Если подключаете свой домен, добавьте его в `DJANGO_ALLOWED_HOSTS` и `DJANGO_CSRF_TRUSTED_ORIGINS`
  сервиса `web` в `.railway/railway.ts`.
