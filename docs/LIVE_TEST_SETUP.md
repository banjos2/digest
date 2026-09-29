# Подключение Telethon, OpenRouter API и Telegram-бота

Инструкция рассчитана на текущий локальный Windows-контур. В результате подключаются три
независимые интеграции:

1. выделенный Telegram-аккаунт читает разрешённые публичные каналы через Telethon;
2. OpenRouter API создаёт русский текст и английский перевод;
3. отдельный Telegram-бот принимает команды пользователей и отправляет ответы.

У аккаунта-сборщика и бота разные реквизиты. `api_id` и `api_hash` нельзя получить у BotFather,
а токен бота нельзя использовать вместо них.

## 1. Открыть папку проекта

Откройте обычный PowerShell. Права администратора не нужны. Выполните:

```powershell
Set-Location "C:\Users\Vladislavv\Documents\ChatGPT\AI-дайджесты бот"
Get-Location
Test-Path .\.venv\Scripts\python.exe
```

Последняя команда должна показать `True`.

## 2. Создать локальный файл секретов

```powershell
New-Item -ItemType Directory -Force .\var | Out-Null
Copy-Item .\config\integration-env.ps1.example .\var\integration-env.ps1
notepad .\var\integration-env.ps1
```

Заполняйте только `var\integration-env.ps1`. Файл `config\integration-env.ps1.example` оставьте
без реальных значений. Папка `var` исключена из Git.

Итоговый файл будет иметь такой вид:

```powershell
$projectRoot = Split-Path -Parent $PSScriptRoot

$env:TELEGRAM_SOURCE_API_ID = "12345678"
$env:TELEGRAM_SOURCE_API_HASH = "ваш_api_hash"
$env:TELEGRAM_SOURCE_PHONE = "+79990000000"
$env:TELEGRAM_SOURCE_SESSION_PATH = Join-Path $projectRoot "var\telethon\source"
$env:TELEGRAM_SOURCE_EDIT_LOOKBACK = "20"

$env:OPENROUTER_API_KEY = "sk-or-v1-..."
$env:OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
$env:OPENROUTER_APP_TITLE = "Owned AI Digest"
$env:AI_RU_MODEL = "openai/gpt-5.4"
$env:AI_EN_MODEL = "openai/gpt-5-mini"
$env:AI_MONTHLY_BUDGET_USD = "5.00"

$env:TELEGRAM_BOT_TOKEN = "1234567890:токен_от_BotFather"
$env:TELEGRAM_WEBHOOK_SECRET = "случайная_секретная_строка"
```

Кавычки должны остаться. Не добавляйте пробелы внутрь значений. Не отправляйте заполненный файл,
ключи, код входа Telegram или файл `.session` в чат.

После каждого изменения сохраните файл (`Ctrl+S`) и подключите его к текущему PowerShell:

```powershell
. .\var\integration-env.ps1
```

Первая точка, пробел и путь — части одной команды. Переменные действуют только в этом PowerShell
и запущенных из него процессах. В новом окне эту команду нужно повторить.

## 3. Подключить выделенный Telegram-аккаунт к Telethon

### 3.1. Подготовить аккаунт

1. Зарегистрируйте выделенный номер в официальном приложении Telegram.
2. Включите для него облачный пароль, то есть двухэтапную аутентификацию.
3. Оставьте официальный клиент авторизованным: туда придёт код первого входа Telethon.

### 3.2. Получить `api_id` и `api_hash`

На странице `https://my.telegram.org/auth`:

1. Введите телефон выделенного аккаунта в международном формате, например `+79990000000`.
2. Нажмите **Next**.
3. Введите код, который Telegram отправит в приложение. Это не токен бота.
4. Откройте **API development tools**.
5. Если приложение ещё не создано, заполните форму:
   - **App title:** `Owned Digest Collector`;
   - **Short name:** `owneddigestcollector`;
   - **Platform:** `Desktop` или ближайший вариант;
   - **Description:** `Internal news collection service`.
6. Сохраните форму.
7. Скопируйте `App api_id` и `App api_hash` в `var\integration-env.ps1`.
8. В `TELEGRAM_SOURCE_PHONE` запишите тот же телефон в международном формате.

`api_id` — число, `api_hash` — длинная строка. `api_hash` храните как пароль.

### 3.3. Создать сессию

Сохраните файл и выполните:

```powershell
. .\var\integration-env.ps1
.\.venv\Scripts\python.exe manage.py integration_preflight
.\.venv\Scripts\python.exe manage.py init_telethon_session
```

Последняя команда запросит код из Telegram и облачный пароль. При вводе пароля символы могут не
отображаться. После входа появится `var\telethon\source.session`. Этот файл предоставляет доступ
к аккаунту; его нельзя пересылать или добавлять в репозиторий.

### 3.4. Проверить каналы без сохранения публикаций

```powershell
.\.venv\Scripts\python.exe manage.py integration_preflight
.\.venv\Scripts\python.exe manage.py probe_telegram_sources `
  --wave A `
  --output research\live_telegram_probe_wave_a.json
```

В preflight ожидается `source_account.ready_for_live_collection: true`. Проверка читает только
метаданные одного последнего сообщения и не сохраняет текст публикации.

Если канал недоступен, откройте его выделенным аккаунтом в официальном Telegram-клиенте. Сборщик
сам не вступает в закрытые каналы и не обходит ограничения.

### 3.5. Разрешить первые источники и собрать материалы

1. Запустите сервер: `.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000 --noreload`.
2. Откройте `http://127.0.0.1:8000/admin/catalog/source/`.
3. Для первого теста откройте `bbcrussian_telegram` и `ainewz_telegram`.
4. Для каждого установите `access_review_status = approved`,
   `collection_check_status = verified`, `preferred_adapter = telethon_public_channel` и
   `is_active = true`.
5. В `access_review_note` запишите, что источник включён во внутренний allowlist, и сохраните.

Сервер занимает текущее окно. Откройте второе окно PowerShell и выполните:

```powershell
Set-Location "C:\Users\Vladislavv\Documents\ChatGPT\AI-дайджесты бот"
. .\var\integration-env.ps1
.\.venv\Scripts\python.exe manage.py collect_source bbcrussian_telegram --limit 10
.\.venv\Scripts\python.exe manage.py collect_source ainewz_telegram --limit 10
```

Результаты появятся в разделе получения материалов в админке. Повторный запуск не создаёт дубли.

## 4. Подключить OpenRouter API

### 4.1. Создать проект и ключ

1. Откройте `https://openrouter.ai/` и войдите в рабочую учётную запись.
2. Откройте `https://openrouter.ai/settings/credits` и пополните баланс на небольшую сумму.
3. Откройте `https://openrouter.ai/settings/keys`.
4. Нажмите **Create API Key** и назовите ключ, например `AI Digest Dev`.
5. Если форма предлагает лимит ключа, установите небольшой месячный предел.
6. Сразу скопируйте ключ вида `sk-or-v1-...`: повторно полностью он не отображается.
7. Вставьте его в `var\integration-env.ps1`:

   ```powershell
   $env:OPENROUTER_API_KEY = "sk-or-v1-..."
   ```

Оставьте модели и локальный тестовый бюджет:

```powershell
$env:OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
$env:OPENROUTER_APP_TITLE = "Owned AI Digest"
$env:AI_RU_MODEL = "openai/gpt-5.4"
$env:AI_EN_MODEL = "openai/gpt-5-mini"
$env:AI_MONTHLY_BUDGET_USD = "5.00"
```

Лимит `$5` в приложении дополняет лимит ключа OpenRouter. Если строка бюджета текущего месяца уже
создана, значение из админки имеет приоритет; измените его в
`http://127.0.0.1:8000/admin/ai_gateway/aibudgetperiod/`.

### 4.2. Проверить настройку

```powershell
. .\var\integration-env.ps1
.\.venv\Scripts\python.exe manage.py integration_preflight
```

Ожидается `ai.provider: openrouter` и `ai.ready_for_live_call: true`. Эта проверка не вызывает
OpenRouter и не расходует деньги.

Первый оплачиваемый RU/EN-прогон после создания проверенного события:

```powershell
.\.venv\Scripts\python.exe manage.py generate_event_cards EVENT_UUID live-test-2026-09
```

Замените `EVENT_UUID` на UUID события из админки. Команда делает два запроса: русский черновик и
английский перевод. Токены и стоимость появятся в журнале AI-запросов.

## 5. Создать и подключить нашего Telegram-бота

### 5.1. Создать бота у BotFather

1. В Telegram найдите официальный верифицированный аккаунт `@BotFather`.
2. Отправьте `/newbot`.
3. Введите отображаемое имя, например `Наш AI-дайджест`.
4. Введите уникальный username, заканчивающийся на `bot`, например
   `our_company_ai_digest_bot`.
5. BotFather пришлёт токен вида `1234567890:AA...`. Скопируйте его в:

   ```powershell
   $env:TELEGRAM_BOT_TOKEN = "1234567890:AA..."
   ```

Если токен опубликован, немедленно отзовите его через BotFather и создайте новый.

### 5.2. Задать меню команд

В `@BotFather` отправьте `/setcommands`, выберите бота и вставьте:

```text
start - Запустить и настроить подписку
settings - Изменить подборки и расписание
digest - Получить последний готовый выпуск
stop - Приостановить рассылку
data - Управление данными
cancel - Отменить текущее действие
help - Помощь
```

Через `/setdescription` можно указать: `Персональные новостные и AI-дайджесты на русском и
английском языке.` Через `/setabouttext`: `Настройте темы и расписание выпуска.`

### 5.3. Создать секрет webhook

Для локального polling он не передаётся Telegram, но приложение требует сохранить его вместе с
токеном. Сгенерируйте секрет:

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

Скопируйте результат в:

```powershell
$env:TELEGRAM_WEBHOOK_SECRET = "сюда_случайную_строку"
```

Не используйте токен бота в качестве webhook-секрета.

### 5.4. Проверить и запустить бота локально

Перед первым запуском откройте `http://127.0.0.1:8000/admin/scheduling/schedule/`. Для расписаний
`twice_daily`, `daily` и `weekly` включите **Показывать новым подписчикам** и
**Рассылка разрешена**. Время должно быть соответственно `10:00, 19:00`, `13:00` и понедельник
`13:00` по `Europe/Moscow`.

```powershell
. .\var\integration-env.ps1
.\.venv\Scripts\python.exe manage.py integration_preflight
.\.venv\Scripts\python.exe manage.py run_telegram_polling
```

Оставьте окно открытым. Затем откройте бота в Telegram, нажмите **Start** или отправьте `/start`,
выберите язык, подборки и расписание. Проверьте `/settings`, `/help`, `/stop` и повторный `/start`.
Команда получает сообщения и сразу отправляет ответы. Остановка — `Ctrl+C`.

Для одного прохода можно отправить боту `/start`, затем выполнить:

```powershell
.\.venv\Scripts\python.exe manage.py run_telegram_polling --once --timeout 0
```

Локальный Django-сервер для polling не требуется. Если команда сообщает об активном webhook,
polling нельзя запускать одновременно с ним. Публичный HTTPS webhook будет подключён при
развёртывании; его путь в приложении — `/telegram/webhook/`.

## 6. Рекомендуемая схема окон PowerShell

**Окно 1 — админка:**

```powershell
Set-Location "C:\Users\Vladislavv\Documents\ChatGPT\AI-дайджесты бот"
. .\var\integration-env.ps1
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000 --noreload
```

**Окно 2 — бот:**

```powershell
Set-Location "C:\Users\Vladislavv\Documents\ChatGPT\AI-дайджесты бот"
. .\var\integration-env.ps1
.\.venv\Scripts\python.exe manage.py run_telegram_polling
```

Для сбора и AI-команд откройте третье окно и также подключите файл переменных.

## 7. Итоговая проверка

- существует `var\integration-env.ps1`, реальные ключи находятся только в нём;
- существует `var\telethon\source.session`;
- `integration_preflight` показывает готовность Telethon, AI и Bot API;
- `probe_telegram_sources` видит тестовые каналы;
- два разрешённых источника собирают публикации;
- бот отвечает на `/start` при работающем `run_telegram_polling`;
- в админке появляется подписчик и редакция его настроек;
- AI-бюджет задан и на стороне OpenRouter, и в приложении.

Для разбора ошибки можно прислать вывод `integration_preflight` и текст ошибки. Они не выводят
секреты. Не присылайте `integration-env.ps1`, токены, `api_hash`, Telegram-коды и `.session`.

Официальные страницы: Telegram — `https://core.telegram.org/api/obtaining_api_id`, Telethon —
`https://docs.telethon.dev/en/stable/basic/signing-in.html`, BotFather —
`https://core.telegram.org/bots/features#botfather`, Bot API —
`https://core.telegram.org/bots/api#getting-updates`; OpenRouter —
`https://openrouter.ai/docs/quickstart`, `https://openrouter.ai/settings/keys`,
`https://openrouter.ai/settings/credits` и
`https://openrouter.ai/docs/guides/features/structured-outputs`.
