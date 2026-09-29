# Передача и развёртывание на Debian с Docker Compose

Этот пакет использует существующие Telegram Bot API, Telethon и OpenRouter-аккаунты. Входящие
команды Telegram получает polling, поэтому публичный webhook не нужен. Админка публикуется только
на `127.0.0.1:8080`; корпоративный reverse proxy должен дать ей HTTPS и доступ из VPN.

## Состав production-контура

- `proxy` — nginx для статики и передачи запросов Django;
- `web` — Django/Gunicorn и админка;
- `db` — PostgreSQL 17 с pgvector;
- `broker` — RabbitMQ 4;
- `worker` — выполнение фоновых заданий;
- `scheduler` — планирование сбора, выпусков, рассылки и обслуживания;
- `telegram-receiver` — получение команд Telegram через polling;
- `telegram-sender` — единая отправка ответов и выпусков;
- `init-app` — миграции, статика, начальный каталог и роли админки.

Сервисы имеют `restart: unless-stopped`, health checks и постоянные тома. Приложение работает от
непривилегированного пользователя UID `10001`.

## 1. Подготовить Debian

Рекомендуется Debian 12 или новее, 2–4 vCPU, 4–8 GB RAM и от 40 GB SSD. Установите Docker Engine,
Docker Compose v2, Git и `age`. Включите запуск Docker после перезагрузки:

```bash
sudo systemctl enable --now docker
docker --version
docker compose version
age --version
```

Разместите проект, например, в `/opt/ai-digest`. Заполненные `.env.production`, каталоги копий и
Telethon-сессия не должны находиться в Git.

## 2. Создать каталоги данных

```bash
sudo install -d -o 10001 -g 10001 -m 750 /srv/ai-digest/backups
sudo install -d -o 10001 -g 10001 -m 750 /srv/ai-digest/erasure-guard
cd /opt/ai-digest
```

Эти каталоги отделены от тома PostgreSQL. Инфраструктура должна дополнительно копировать их в
другое хранилище или подключить вместо них корпоративное резервное хранилище.

## 3. Подготовить шифрование копий

Создайте ключ `age` в защищённом каталоге, который не входит в проект:

```bash
sudo install -d -m 700 /srv/ai-digest/secrets
sudo age-keygen -o /srv/ai-digest/secrets/backup-age-identity.txt
sudo chmod 600 /srv/ai-digest/secrets/backup-age-identity.txt
sudo age-keygen -y /srv/ai-digest/secrets/backup-age-identity.txt
```

Последняя команда выводит публичный recipient вида `age1...`. Его можно записать в
`BACKUP_AGE_RECIPIENT`. Приватный файл нужен только для проверки восстановления и хранится
отдельно от копий.

## 4. Заполнить production environment

```bash
cp .env.production.example .env.production
chmod 600 .env.production
nano .env.production
```

Обязательно замените все `replace-with...`, домен и контакт в `SOURCE_HTTP_USER_AGENT`.
Перенесите значения Telegram и OpenRouter из локального `var/integration-env.ps1` вручную через
корпоративное хранилище секретов. Не копируйте сам PowerShell-файл на сервер.

Независимые ключи Django можно получить так:

```bash
openssl rand -hex 48
openssl rand -hex 48
```

Первое значение используется как `DJANGO_SECRET_KEY`, второе — как `ERASURE_HASH_KEY`.

Если пароль RabbitMQ содержит специальные символы, URL-кодируйте их в `CELERY_BROKER_URL`.
Значение `RABBITMQ_PASSWORD` остаётся обычным паролем.

Проверка итогового Compose без запуска:

```bash
docker compose --env-file .env.production -f compose.production.yaml config --quiet
```

## 5. Собрать образ и поднять инфраструктуру

```bash
docker compose --env-file .env.production -f compose.production.yaml build
docker compose --env-file .env.production -f compose.production.yaml up -d db broker
docker compose --env-file .env.production -f compose.production.yaml run --rm init-app
```

`init-app` применяет миграции, собирает статику, импортирует исходный каталог без перезаписи
административных изменений и синхронизирует группы доступа.

## 6. Авторизовать существующий Telethon-аккаунт

Предпочтительный вариант — создать новую session непосредственно в постоянном Docker-томе:

```bash
docker compose --env-file .env.production -f compose.production.yaml --profile tools run --rm app-admin python manage.py init_telethon_session
```

Введите код Telegram и, если потребуется, пароль двухэтапной аутентификации. Эти значения не
сохраняются в `.env.production`.

Альтернатива — защищённо скопировать существующий файл `source.session` в том
`telethon_session`. Это должен делать специалист, отвечающий за секреты; session-файл равнозначен
доступу к Telegram-аккаунту.

## 7. Инициализировать защиту доставки и администратора

До первого запуска sender создайте начальный зашифрованный erasure guard:

```bash
docker compose --env-file .env.production -f compose.production.yaml --profile tools run --rm app-admin python manage.py export_erasure_guard
```

Создайте production-суперпользователя интерактивно:

```bash
docker compose --env-file .env.production -f compose.production.yaml --profile tools run --rm app-admin python manage.py createsuperuser
```

Проверьте конфигурацию интеграций:

```bash
docker compose --env-file .env.production -f compose.production.yaml --profile tools run --rm app-admin python manage.py integration_preflight
```

Ожидается PostgreSQL, настроенный OpenRouter, существующая Telethon-сессия и готовый delivery
guard. Команда не печатает секретные значения.

## 8. Запустить весь сервис

```bash
docker compose --env-file .env.production -f compose.production.yaml up -d
docker compose --env-file .env.production -f compose.production.yaml ps
docker compose --env-file .env.production -f compose.production.yaml logs --tail=100
```

`telegram-receiver` при старте удаляет старый webhook без удаления накопленных updates и переходит
на polling. Отправкой занимается только `telegram-sender`.

Проверка локального reverse proxy на сервере:

```bash
curl -fsS -H 'Host: digest.company.example' -H 'X-Forwarded-Proto: https' http://127.0.0.1:8080/health/
```

Вместо `digest.company.example` укажите домен из `DJANGO_ALLOWED_HOSTS`. Ожидаемый ответ:
`{"status":"ok","scope":"web_and_database"}`.

## 9. Подключить корпоративный HTTPS

Корпоративный reverse proxy направляет запросы на `127.0.0.1:8080`, передаёт исходный `Host`,
`X-Forwarded-For` и `X-Forwarded-Proto: https`. В `.env.production` должны совпадать:

```dotenv
DJANGO_ALLOWED_HOSTS=digest.company.example
DJANGO_CSRF_TRUSTED_ORIGINS=https://digest.company.example
DJANGO_TRUST_PROXY=1
```

Порт PostgreSQL, RabbitMQ management и Gunicorn наружу не публикуются.

## 10. Проверить автоматический цикл

1. Войдите в админку по корпоративному HTTPS-адресу.
2. Проверьте активные источники, подборки, расписания и AI-бюджет.
3. В Telegram выполните `/start`, `/settings` и `/digest`.
4. Проверьте процессы и последние задания на главной странице админки.
5. Дождитесь одного автоматического цикла сбора и тестового выпуска.
6. Проверьте, что после `sudo reboot` все контейнеры вернулись в состояние healthy.

Полезные команды:

```bash
docker compose --env-file .env.production -f compose.production.yaml ps
docker compose --env-file .env.production -f compose.production.yaml logs -f scheduler worker telegram-receiver telegram-sender
docker compose --env-file .env.production -f compose.production.yaml --profile tools run --rm app-admin python manage.py check --deploy
```

## 11. Обновление версии

Перед обновлением создайте копию базы. Затем:

```bash
git pull --ff-only
docker compose --env-file .env.production -f compose.production.yaml build
docker compose --env-file .env.production -f compose.production.yaml run --rm init-app
docker compose --env-file .env.production -f compose.production.yaml up -d --remove-orphans
docker compose --env-file .env.production -f compose.production.yaml ps
```

## 12. Проверка восстановления

Ежедневный scheduler создаёт зашифрованную копию PostgreSQL и отдельный erasure guard. Приватный
ключ `age` в обычные контейнеры не монтируется. Для restore drill разработчик монтирует его только
в одноразовый `app-admin` и использует процедуру из `docs/RESTORE_RUNBOOK.md`.

До допуска пользователей необходимо хотя бы один раз восстановить копию в отдельную PostgreSQL
базу и зафиксировать результат.

## 13. Известный продуктовый пункт перед широким использованием

Текущая версия создаёт профиль любому Telegram-пользователю, который найдёт username бота.
Размещение можно провести и проверить закрытой группой, но перед распространением username внутри
организации разработчику нужно добавить управляемый допуск сотрудников: allowlist Telegram ID или
одноразовые приглашения. Это изменение приложения, а не инфраструктуры.

## 14. Локальные данные

По умолчанию production начинает с чистой базы и исходного каталога. Тестовые подписчики, очереди,
выпуски и AI-журналы переносить нельзя. Если нужно сохранить сделанные в локальной админке
изменения источников, подборок и расписаний, разработчик должен экспортировать только модели
`catalog`, `Schedule`, `ScheduleRevision` и `AIBudgetPeriod`, проверить fixture и загрузить её до
первого запуска scheduler.
