# Развёртывание в Dokploy

Dokploy запускает тот же [`compose.production.yaml`](../compose.production.yaml), что и ручная установка на
Debian: PostgreSQL 17, RabbitMQ, миграции (`init-app`), web, nginx (`proxy`), worker, scheduler, приём и
отправка Telegram. Переменные Dokploy пишет в `.env`, compose читает его автоматически.

Нужны: сервер с установленным Dokploy, SSH-доступ к нему и домен с A-записью на IP сервера.

## 1. Подготовить сервер (SSH)

Каталоги для резервных копий и erasure guard принадлежат пользователю приложения (UID `10001`), ключ
шифрования копий создаётся на сервере:

```bash
sudo install -d -o 10001 -g 10001 -m 750 /srv/ai-digest/backups
sudo install -d -o 10001 -g 10001 -m 750 /srv/ai-digest/erasure-guard
sudo apt-get install -y age
sudo install -d -m 700 /srv/ai-digest/secrets
sudo age-keygen -o /srv/ai-digest/secrets/backup-age-identity.txt
sudo age-keygen -y /srv/ai-digest/secrets/backup-age-identity.txt
```

Последняя команда печатает публичный ключ `age1...` для `BACKUP_AGE_RECIPIENT`. Копию приватного
ключа сохраните вне сервера: без неё резервные копии не расшифровать.

## 2. Создать сервис

1. **Projects → Create Project** (например, `ai-digest`) → **Create Service → Compose**.
2. **General → Provider:** GitHub, репозиторий `banjos2/digest`, ветка `main`,
   **Compose Path** `./compose.production.yaml`. Для приватного репозитория сначала подключите GitHub в
   **Settings → Git**.
3. **Advanced:** включите **Isolated Deployment**, чтобы Traefik видел сервис `proxy` без правок compose.

## 3. Переменные

Сгенерируйте случайные значения (на своём компьютере):

```bash
for n in DJANGO_SECRET_KEY ERASURE_HASH_KEY TELEGRAM_WEBHOOK_SECRET; do echo "$n=$(openssl rand -hex 32)"; done
```

```bash
for n in POSTGRES_PASSWORD RABBITMQ_PASSWORD; do echo "$n=$(openssl rand -hex 24)"; done
```

Во вкладку **Environment** вставьте и заполните (`digest.example.com` замените своим доменом):

```dotenv
DJANGO_SECRET_KEY=
ERASURE_HASH_KEY=
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=digest.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://digest.example.com
DJANGO_TRUST_PROXY=1

POSTGRES_HOST=db
POSTGRES_DB=digest
POSTGRES_USER=digest
POSTGRES_PASSWORD=
RABBITMQ_USER=digest
RABBITMQ_PASSWORD=
# Тот же RABBITMQ_PASSWORD, что строкой выше.
CELERY_BROKER_URL=amqp://digest:ПАРОЛЬ_RABBITMQ@broker:5672//

SOURCE_HTTP_USER_AGENT=OwnedDigestBot/1.0 (+mailto:ваш-контакт@example.com)
OPENROUTER_API_KEY=
AI_MONTHLY_BUDGET_USD=25.00

TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
# Для сбора из Telegram-каналов; иначе оставьте пустыми.
TELEGRAM_SOURCE_API_ID=
TELEGRAM_SOURCE_API_HASH=
TELEGRAM_SOURCE_PHONE=
TELEGRAM_SOURCE_SESSION_PATH=/run/secrets/telethon/source

BACKUP_HOST_PATH=/srv/ai-digest/backups
ERASURE_GUARD_HOST_PATH=/srv/ai-digest/erasure-guard
BACKUP_OUTPUT_DIR=/backups
ERASURE_GUARD_OUTPUT_DIR=/erasure-guard
BACKUP_SCHEDULE_ENABLED=1
BACKUP_AGE_RECIPIENT=age1...
```

Остальные настройки берутся из значений по умолчанию; полный список — в
[`.env.production.example`](../.env.production.example). Если порт `127.0.0.1:8080` на сервере занят,
добавьте `APP_HTTP_PORT` с другим портом.

## 4. Домен и запуск

1. **Domains → Add Domain:** Service `proxy`, Host — ваш домен, Path `/`, Container Port `8080`,
   HTTPS включён, Certificate `Let's Encrypt`.
2. **Deploy.** Первая сборка занимает 5–10 минут; `init-app` применит миграции, загрузит каталог и роли.
   Ход смотрите в **Deployments** и **Logs**.
3. Проверка: `https://ваш-домен/health/` отвечает `{"status": "ok", ...}`.

## 5. Администратор и Telethon

В **Terminal** сервиса выберите контейнер `web` и выполните:

```bash
python manage.py createsuperuser
```

Если нужен сбор из Telegram-каналов, в контейнере `worker` один раз выполните
`python manage.py init_telethon_session` (код придёт в Telegram).

Обновления: пуш в `main`, затем **Deploy** (или включите **Autodeploy** во вкладке General).
