# DNS Resolver + MikroTik Routes

Веб-приложение на Flask для домашней инфраструктуры:
- резолвит A-записи доменов через выбранный DNS-провайдер (по умолчанию Google DNS)
- принимает много доменов (по одному в строке)
- показывает прогресс резолва в модальном окне (`resolving <domain>`, прогресс `N / M`)
- добавляет только недостающие маршруты в MikroTik
- поддерживает журнал изменений и откат (одиночный и пакетный)

## Что умеет

- Форма с полями:
  - `Gateway` (выпадающий список)
  - `Distance`
  - `Comment Prefix` (опционально)
  - `Домены` (многострочное поле)
- Кнопка `Найти адреса` запускает асинхронный резолв и показывает live-прогресс.
- При изменении `Comment Prefix` комментарии в таблице найденных IP обновляются сразу (без повторного резолва).
- Перед добавлением маршрутов есть подтверждение.
- После POST используется `POST -> Redirect -> GET`, поэтому при `F5 / Cmd+R` всегда открывается `/`.

## Логика добавления маршрутов

- Для каждого домена запрашиваются A-записи через выбранный DNS-провайдер.
- Комментарий для маршрута:
  - `<prefix>: <dns-name>` если заполнен `Comment Prefix`
  - `<dns-name>` если `Comment Prefix` пустой
- Проверка существующих маршрутов идет строго в рамках выбранных `gateway` и `distance`.
- Если IP уже покрыт существующим маршрутом (включая подсеть), он не добавляется и показывается как `Пропущен` с деталями покрытия.
- Если IP не покрыт, добавляется маршрут:
  - `dst-address=<ip>/32`
  - `gateway=<выбранный gateway>`
  - `distance=<выбранный distance>`
  - `comment=<итоговый комментарий>`

## Журнал и откат

- Каждое добавление маршрута пишется в SQLite (`AUDIT_DB_PATH`).
- Для каждой записи хранится источник запроса (IP/hostname), параметры маршрута и статус (`active`/`rolled_back`).
- В окне логов есть:
  - чекбокс в каждой строке
  - чекбокс «выбрать все»
  - кнопка `Откатить выбранные` для пакетного отката

## Конфигурация через переменные окружения

### Обязательные
- `APP_SECRET_KEY` — секрет Flask сессий.
- `GATEWAYS` — список доступных gateway/interface для UI.
- `MIKROTIK_USERNAME` — логин API пользователя MikroTik.
- `MIKROTIK_PASSWORD` — пароль API пользователя MikroTik.

### Рекомендуемые
- `MIKROTIK_HOST` (по умолчанию `gateway.home`)
- `MIKROTIK_PORT` (по умолчанию `18728`)
- `MIKROTIK_SSL` (`true/false`, по умолчанию `false`)

### Опциональные
- `APP_PORT` (по умолчанию `5000`)
- `DNS_PROVIDERS` — список DNS провайдеров в UI.
- `DEFAULT_DNS_PROVIDER` (по умолчанию `google`)
- `DEFAULT_DISTANCE` (по умолчанию `20`)
- `DEFAULT_COMMENT_PREFIX` (по умолчанию пусто)
- `DNS_REQUEST_TIMEOUT_SECONDS` (по умолчанию `3`)
- `RESOLVE_MAX_DURATION_SECONDS` (по умолчанию `55`)
- `RESOLVE_CACHE_DIR` (по умолчанию `/tmp/dns_resolver_resolves`)
- `RESOLVE_CACHE_TTL_SECONDS` (по умолчанию `1800`)
- `INDEX_CONTEXT_CACHE_DIR` (по умолчанию `/tmp/dns_resolver_index_context`)
- `INDEX_CONTEXT_TTL_SECONDS` (по умолчанию `900`)
- `RESOLVE_JOB_TTL_SECONDS` (по умолчанию `3600`)
- `AUDIT_DB_PATH` (по умолчанию `/tmp/dns_resolver_audit.db`)
- `AUDIT_LOG_MAX_ENTRIES` (по умолчанию `200`)
- `GUNICORN_WORKERS` (по умолчанию `1`)
- `GUNICORN_TIMEOUT` (по умолчанию `180`)

### Пример `.env`

```env
APP_PORT=5000
APP_SECRET_KEY=replace_with_long_random_secret

GATEWAYS=192.168.222.201|default|Отправить в VPN,Infolink-eth4||Исключить из VPN,192.168.61.1
DEFAULT_DISTANCE=20
DEFAULT_COMMENT_PREFIX=

DNS_PROVIDERS=google|Google DNS|https://dns.google/resolve,cloudflare|Cloudflare DNS|https://cloudflare-dns.com/dns-query,yandex|Yandex DNS|ns://77.88.8.8,system|System DNS|system
DEFAULT_DNS_PROVIDER=google
DNS_REQUEST_TIMEOUT_SECONDS=3
RESOLVE_MAX_DURATION_SECONDS=55

MIKROTIK_HOST=gateway.home
MIKROTIK_PORT=18728
MIKROTIK_USERNAME=api_user
MIKROTIK_PASSWORD=strong_password
MIKROTIK_SSL=false

RESOLVE_CACHE_DIR=/tmp/dns_resolver_resolves
RESOLVE_CACHE_TTL_SECONDS=1800
INDEX_CONTEXT_CACHE_DIR=/tmp/dns_resolver_index_context
INDEX_CONTEXT_TTL_SECONDS=900
RESOLVE_JOB_TTL_SECONDS=3600

AUDIT_DB_PATH=/data/audit.db
AUDIT_LOG_MAX_ENTRIES=200

GUNICORN_WORKERS=1
GUNICORN_TIMEOUT=180
```

## Формат `GATEWAYS`

`GATEWAYS` задается через запятую.

Форматы элемента:
- `gateway`
- `gateway|default|label`
- `gateway||label`

Где:
- `gateway` — реальное значение, которое отправляется в MikroTik
- `default` — выбрать по умолчанию на форме
- `label` — отображаемое имя в UI

Пример:
```env
GATEWAYS=192.168.222.201|default|Отправить в VPN,Infolink-eth4||Исключить из VPN,192.168.61.1
```

## Формат `DNS_PROVIDERS`

`DNS_PROVIDERS` задается через запятую.

Формат элемента: `key|label|endpoint`

- `key` — внутренний идентификатор
- `label` — отображаемое имя в UI
- `endpoint`:
  - `https://dns.google/resolve` (Google JSON API)
  - `https://.../dns-query` (DoH endpoint)
  - `ns://<ip>` или `ns://<ip>:<port>` (прямой DNS по UDP/TCP)
  - `system` (системный DNS контейнера)

Пример:
```env
DNS_PROVIDERS=google|Google DNS|https://dns.google/resolve,cloudflare|Cloudflare DNS|https://cloudflare-dns.com/dns-query,yandex|Yandex DNS|ns://77.88.8.8,system|System DNS|system
DEFAULT_DNS_PROVIDER=google
```

## Локальный запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Открыть: `http://127.0.0.1:5000`

## Docker Compose

Пример с внешним volume для персистентного SQLite:

```yaml
services:
  dns-resolver:
    image: mikhailpyrochkin/dns-resolver:latest
    container_name: dns_resolver
    env_file:
      - .env
    environment:
      - AUDIT_DB_PATH=/data/audit.db
    ports:
      - "8811:5000"
    volumes:
      - dns_resolver_data:/data
    restart: unless-stopped

volumes:
  dns_resolver_data:
```

Запуск:
```bash
docker compose up -d
```

Открыть: `http://127.0.0.1:8811`

## Docker Run

Вариант с именованным volume:

```bash
docker volume create dns_resolver_data

docker run -d \
  --name dns_resolver \
  --restart unless-stopped \
  --env-file .env \
  -e AUDIT_DB_PATH=/data/audit.db \
  -p 8811:5000 \
  -v dns_resolver_data:/data \
  mikhailpyrochkin/dns-resolver:latest
```

Вариант с bind-mount на хосте:

```bash
mkdir -p ./dns_resolver_data

docker run -d \
  --name dns_resolver \
  --restart unless-stopped \
  --env-file .env \
  -e AUDIT_DB_PATH=/data/audit.db \
  -p 8811:5000 \
  -v "$(pwd)/dns_resolver_data:/data" \
  mikhailpyrochkin/dns-resolver:latest
```
