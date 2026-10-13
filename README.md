# Magaz UI — дашборд обновления 1С в магазинах

Монорепозиторий проекта по спецификации [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md)
(версия **1.5**). Документ — единственный источник истины: структура репозитория (§6),
модель данных (§4), API (§5), инварианты (§10), план этапов (§12).

**Статус: этапы Э1 «Каркас» и Э2 «Канал связи» — выполнены** (отчёты — в конце этого файла).

## Состав репозитория

| Каталог | Что внутри |
|---|---|
| `server/` | FastAPI-сервер дашборда: модели §4, Alembic-миграции, Admin API, Agent API, отдача SPA |
| `client/` | Агент кассы (`MagazAgent` + TrayAgent): в Э2 — конфиг §7.2, `GET /`, `/health`, `/version`, CLI; ядро 1С и уведомления — Э3–Э5 |
| `frontend/` | SPA-дашборд: Vue 3 + Vite + TypeScript + PrimeVue/Tailwind (решение П-4) |
| `shared/` | Общий код сервера и агента: схемы, версия, работа с путями и временем |
| `tests/` | `pytest` — `tests/server/`, `tests/shared/`; Vitest — `frontend/tests/` |
| `docs/` | Спецификация (остальные документы — `API.md`, `INSTALL.md`, `RUNBOOK.md` — на Э4/Э6) |
| `deploy/` | Скрипты установки и сборки (наполняется с Э6) |

## Быстрый старт: сервер

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt

.venv\Scripts\python.exe -m alembic upgrade head        # создаёт server\data\magaz.db
.venv\Scripts\python.exe -m server.cli create-admin --username admin   # пароль спросит интерактивно
.venv\Scripts\python.exe -m server.main                 # http://127.0.0.1:8080
```

Конфигурация — переменные окружения с префиксом `MAGAZ_` и/или файл `.env` в корне
(`server/config.py`): `MAGAZ_HOST`, `MAGAZ_PORT`, `MAGAZ_DATABASE_URL`, `MAGAZ_LOG_LEVEL`,
`MAGAZ_SECRET_KEY`, `MAGAZ_COOKIE_SECRET`, `MAGAZ_SERVE_SPA` и др.

Служебные команды (§14.1): `create-admin`, `list-admins`, `reset-password`, `seed-settings`,
`info` — `python -m server.cli <команда> --help`.

Рабочие файлы сервера (`server\data\`): БД `magaz.db`, ключ шифрования паролей 1С
`secrets.key`, ключ cookie `cookie_secret.key`, логи `logs\server.log`. Каталог **не**
коммитится и создаётся автоматически при старте (инвариант I-10: импорт модулей без
сайд-эффектов).

## Быстрый старт: фронтенд

```powershell
cd frontend
npm ci
npm run build      # vue-tsc --noEmit + vite build → frontend\dist (её отдаёт сервер)
npm run dev        # режим разработки, /api проксируется на http://127.0.0.1:8080
```

Node нужен **только на сборке**: сервер отдаёт уже собранный `frontend\dist`
(`server/api/spa.py`: статика + history-fallback для маршрутов SPA). Пока `dist` не собран,
на `/` отдаётся страница-заглушка `503` с инструкцией по сборке, а `/api/v1/*` работает штатно.

## Быстрый старт: агент кассы (Э2)

1. В дашборде создать магазин (`/stores` → «Добавить магазин») и скопировать блок
   «Что вписать в конфиг агента».
2. Положить `agent-config.json` (§7.2) рядом с агентом (по умолчанию `C:\MagazAgent\`):

```json
{
  "store_id": 1,
  "store_name": "Магазин №5",
  "server_url": "http://10.10.0.1:8080",
  "agent_token": "<из дашборда>",
  "port": 8383,
  "base_1c_path": "C:/Program Files (x86)/1cv8",
  "default_file_name": "update",
  "devices_version": 1,
  "allowed_server_ips": ["10.10.0.1"]
}
```

3. Запустить агент (пока без службы Windows — она появится на Э6):

```powershell
.venv\Scripts\python.exe -m client.agent --console          # лог в терминал
.venv\Scripts\python.exe -m client.agent info               # что прочитано из конфига
.venv\Scripts\python.exe -m client.agent --config C:\MagazAgent\agent-config.json
```

Через интервал мониторинга (15 с) магазин в дашборде станет «онлайн»; кнопка
«Проверить сейчас» в карточке делает живой `GET /health` и показывает `busy`, сессии,
рабочую папку и `base_1c_path`.

## Тесты

```powershell
.venv\Scripts\python.exe -m pytest          # pytest.ini: tests\, asyncio_mode=auto
cd frontend; npm run test                   # Vitest (jsdom) — frontend\tests\
```

Тесты изолированы: временная БД и временные пути ключей (`tests/server/conftest.py`),
реальная `server\data` не изменяется.

---

## Отчёт по этапу Э1 «Каркас»

DoD (§12): `pytest` проходит; `npm run build` собирает SPA; сервер поднимается; БД создаётся
с нуля; вход в дашборд работает.

### Что сделано

**Сервер**

- `server/config.py` — настройки §14.1 п. 4 (`MAGAZ_*` + `.env`), пути БД/логов/ключей,
  синглтон `get_settings()`; импорт модуля без сайд-эффектов (I-10).
- `server/db/models.py` — все таблицы §4, включая `store_devices` (Д-1) и
  `stores.devices_version` (Д-2), шифруемое поле пароля 1С (`stores.password_enc`).
- Alembic (`server/migrations/`, `alembic.ini`): ревизия `d9a388454dc8_initial_schema`.
  Проверка «модели == схема» (`alembic revision --autogenerate` даёт пустую ревизию) —
  расхождений нет.
- `server/api/admin.py` — сессии администратора (`/admin/auth/login|logout|me`),
  `/admin/system/info`, CRUD настроек `/admin/settings`, аудит `/admin/audit`;
  защищённые роуты собираются через `protected_router`.
- `server/api/agent_report.py` — `POST /api/v1/agent/health` (самодоклад: `last_seen_at`,
  версии агента, расхождение учётных данных П-1 и списка устройств Д-1).
- `server/api/spa.py` — отдача `frontend\dist` (статика + history-fallback) и страница
  `503` с инструкцией по сборке, пока `dist` отсутствует.
- `server/main.py` — `create_app()` + `lifespan` (каталоги, ключи, проверка готовности
  схемы, посев настроек §4.9 — 12 ключей).
- `server/cli.py` — `create-admin`, `list-admins`, `reset-password`, `seed-settings`, `info`.
- `server/core/` — контекст приложения, безопасность (Fernet + itsdangerous-подпись cookie),
  логирование с ротацией.
- `requirements.txt` / `requirements-dev.txt` — версии зафиксированы (`fastapi==0.143.0`,
  `SQLAlchemy==2.1.4`, `alembic==1.20.0`, `pydantic==2.14.1`, `cryptography==50.0.2`,
  `pytest==9.1.1`).

**SPA (Vue 3 + Vite + TypeScript + PrimeVue Aura + Tailwind)**

- `src/router/index.ts` — все маршруты §5.2.1 + защита через `createAppRouter(pinia, history)`;
  страницы, запланированные на Э2–Э5, отдают `StagePlaceholder` с описанием будущей функции.
- `src/api/{client,endpoints,types}.ts` — fetch-клиент (`ApiError`, обработка `401`,
  флаг `skipUnauthorizedHandler` для `/me`), типизированные вызовы.
- `src/stores/{auth,system}.ts` (Pinia) — сессия и опрос `/system/info` раз в 10 с.
- Экраны: `/login` (рабочий), `/` (дашборд: плитки статуса, счётчики), `/settings`
  (настройки §4.9), `/audit` (журнал), `404`; каркас меню и шапки — `AppShell.vue`.

### Критерии готовности — факты

| Критерий | Проверка | Результат |
|---|---|---|
| `pytest` проходит | `python -m pytest -q` | код возврата `0`; собрано **161** тест (server 96, shared 65) |
| Frontend-тесты | `npm run test` | **25** тестов в 4 файлах — все зелёные |
| `npm run build` собирает SPA | `vue-tsc --noEmit` + `vite build` | без ошибок типов, `frontend\dist` собран (~340 кБ JS, ~29 кБ CSS) |
| Сервер поднимается | `python -m server.main` | uvicorn на `0.0.0.0:8080`, `GET /api/v1/health` → `{"status":"online","app_name":"Magaz UI","app_version":"1.0.0"}` |
| БД создаётся с нуля | `alembic upgrade head` + autogenerate | `server\data\magaz.db` создана, ревизия `d9a388454dc8`, дрейфа нет |
| Вход в дашборд работает | `POST /api/v1/admin/auth/login` → `/auth/me` → `/admin/settings` | `200` + подписанная cookie; без cookie — `401`; в аудите событие `admin_login` |
| SPA отдаётся сервером | `GET /`, `GET /stores/12`, ассеты | `200` (index.html, history-fallback, `/assets/*`) |
| Настройки §4.9 | `GET /admin/settings` | 12 ключей, значения по умолчанию засеяны при старте |
| CLI администратора | `create-admin` / `list-admins` / `info` | администратор создан, `info` показывает БД, `frontend\dist`, адрес и версию |

### Отклонения и решения, принятые по ходу этапа

1. **Д-2 (добавка в спецификацию).** §7.6 и §5.1.2 требуют сверять `devices_version`, но
   в §4.1 такого поля у `stores` не было. Добавлено
   `stores.devices_version` (INTEGER, по умолчанию 1); спецификация поднята до версии 1.4
   (§4.1 и Приложение D).
2. **Стиль заглушек.** Страницы будущих этапов отдают `StagePlaceholder` с перечнем того,
   что появится на этапе, — чтобы навигация была проверяема уже сейчас.
3. **Ожидание сессии в `App.vue`.** Оболочка с меню не монтируется, пока сессия не
   проверена, иначе она успевает опросить защищённые эндпоинты и в консоль летят лишние
   `401`. На время проверки показывается «Проверяем сессию…».
4. **`logout` не падает при недоступном сервере.** Ошибка выхода гасится: локальное
   состояние всё равно сбрасывается, иначе пользователь «застревает» в интерфейсе.
5. **`@types/node` в devDependencies** — нужен `vite.config.ts` для `node:url` при
   `vue-tsc --noEmit`.

### Что дальше (Э2 «Канал связи»)

Health Monitor, CRUD магазинов и выдача токена, `GET /health` и `/version` у агента,
эндпоинт самодоклада, страница `/stores`.

---

## Отчёт по этапу Э2 «Канал связи»

DoD (§12): магазин, поднятый локально с токеном, отображается «онлайн»; без ответа — «офлайн»;
подмена токена/IP даёт `401`/`403`.

### Что сделано

**Сервер**

- `server/core/client_http.py` — единая точка исходящих вызовов к агентам (§6, I-11):
  `AgentTarget` (адрес и токен из `stores`), `AgentClient` (`get_health`, `get_version`,
  низкоуровневый `request` — им будет пользоваться диспетчер на Э4), типизированные ошибки
  (`AgentOfflineError` / `AgentAuthError` / `AgentProtocolError`) и инжектируемый транспорт.
  Обращения идут напрямую: `trust_env=False` (см. отклонения ниже).
- `server/core/health_monitor.py` — Health Monitor (§3.2, §3.3): параллельный опрос с семафором
  `health_concurrency`, таймаут `health_timeout_sec`, запись `last_seen_at`, `agent_version`,
  `os_info` в `stores`; «онлайн» считается по окну `online_window_sec` в момент чтения, а не
  хранится. Настройки читаются из БД на каждом цикле — изменение в UI применяется без
  перезапуска. Запуск/остановка — из `lifespan`.
- `server/services/store_service.py` — CRUD магазинов, проверка уникальности имени/IP,
  генерация и перевыпуск `agent_token`, учётные данные 1С (П-1: Fernet + рост
  `credentials_version`), фильтры списка и счётчики онлайн/офлайн.
- `server/api/stores.py` — `/api/v1/admin/stores` (список/создание/карточка/изменение/мягкое
  удаление), `POST /stores/{id}/token/rotate`, `PUT /stores/{id}/credentials` (Д-5),
  `POST /stores/{id}/check` (Д-3); все изменения — в `audit_log`, пароль в аудит не попадает.
- `server/api/admin.py` — счётчики `stores_online` / `stores_offline` / `online_window_sec`
  в `/admin/system/info` (плитки главной, FR-13).
- `server/main.py` — старт/остановка мониторинга, транспорт к агентам через
  `app.state.agent_transport` (подмена в тестах без правок бизнес-кода).

**Агент кассы (`client/agent/`)**

- `config.py` — §7.2 плюс `devices_version` и `allowed_server_ips` (Д-4); рабочая папка и путь
  конфига вычисляются от места запуска exe (`get_application_root()`, I-14).
- `app.py` — `GET /` (перенос v2 1:1, без токена), `GET /health` (§5.1.2: версия, ОС, пути,
  `busy`, сессии, версии П-1/Д-1), `GET /version`; `X-Agent-Token` → `401`, чужой источник при
  заданном `allowed_server_ips` → `403`, токен не настроен → `503`.
- `__main__.py` — CLI `run` / `--console` / `info`; команды службы
  (`install|remove|start|stop|debug`) честно отвечают, что появятся на Э6.
- `logging_setup.py` — лог службы с ротацией (`C:\ProgramData\MagazAgent\logs\agent.log`,
  переопределяется `MAGAZ_AGENT_LOG_DIR`).

**SPA**

- `src/views/StoresView.vue` — таблица магазинов с фильтрами (поиск, кластер, статус,
  «показывать отключённые»), CRUD-диалог, блок «что вписать в конфиг агента» с токеном и
  JSON-конфигом, перевыпуск токена с предупреждением, отключение/включение магазина.
- `src/views/StoreDetailView.vue` — карточка: реквизиты, состояние канала связи, «Проверить
  сейчас» (`busy`, сессии, аптайм, рабочая папка, `base_1c_path`, задержка), учётные данные
  П-1 («задан» / «заменить» / «очистить»).
- `src/stores/stores.ts` — Pinia-стор с автообновлением раз в 10 с; главная показывает плитки
  «Всего/Онлайн/Офлайн» и раскрывающийся список магазинов (§11.1).

### Критерии готовности — факты

| Критерий | Проверка | Результат |
|---|---|---|
| `pytest` проходит | `python -m pytest -q` | код возврата `0`; **251** тест: `client` 18, `server` 168, `shared` 65 |
| Frontend-тесты | `npm run test` | **40** тестов в 6 файлах — все зелёные |
| `npm run build` собирает SPA | `vue-tsc --noEmit` + `vite build` | без ошибок типов; `dist` собран (index ~344 кБ, `StoresView` ~14 кБ, `StoreDetailView` ~11.8 кБ) |
| Магазин с токеном → «онлайн» | живой прогон: `python -m client.agent` и `POST /admin/stores` (`ip=127.0.0.1`), интервал 3 с, окно 8 с | через цикл: `is_online=true`, `agent_version=1.0.0`, `os_info=Windows 11 (10.0.26200)`, `last_seen_at` заполнен |
| «Проверить сейчас» (Д-3) | `POST /admin/stores/{id}/check` при живом агенте | `200`: `status=online`, задержка ≈750 мс, `work_dir` с кассы, `busy.merge=false` |
| Агент остановлен → «офлайн» | процесс агента остановлен, ожидание окна | `is_online=false`; `check` → `503` «Магазин недоступен (офлайн)»; счётчики: всего 1 / онлайн 0 / офлайн 1 |
| Подмена токена агента | `GET /health` агента с чужим токеном | `401` |
| Ротация токена | `POST .../token/rotate`, затем самодоклад старым токеном | старый токен → `401`, новый работает; сервер→агент со старым конфигом → `502` (агенту нужна перенастройка) |
| Чужой IP источника | самодоклад с `X-Forwarded-For: 10.99.99.99` | `403` |
| Аудит действий | `GET /admin/audit` после прогона | `store_create`, `settings_update`, `store_token_rotate`, `admin_login`; пароля в журнале нет |

### Отклонения и решения, принятые по ходу этапа

1. **Д-3, Д-4, Д-5 (добавки в спецификацию; версия документа 1.5).**
   - Д-3: `POST /api/v1/admin/stores/{id}/check` — «Проверить сейчас»: §11.2 требует показывать
     `busy`, сессии и рабочие пути, а колонок под них в `stores` нет, значит их нужно запрашивать
     живьём (ответ агента ошибкой → `502`, недоступен → `503`; код `502` добавлен в Приложение B).
   - Д-4: канал связи агента — `GET /`, `/health`, `/version`; в `agent-config.json` добавлены
     `devices_version` (сверка списка оборудования, Д-1/Д-2) и `allowed_server_ips` (вторая линия
     защиты из §5.1: список адресов сервера; пусто — проверка выключена).
   - Д-5: `PUT /api/v1/admin/stores/{id}/credentials` — задать/заменить/очистить учётные данные 1С
     (§8.3 требовал кнопки, но точки их применения в API не было).
2. **`trust_env=False` в клиенте агентов (найдено живым прогоном).** На машине разработки в
   настройках ОС зарегистрирован локальный прокси, и `httpx` по умолчанию отправлял туда даже
   обращения к `127.0.0.1:8383` — отвечал прокси (`503` с пустым телом), а не агент. Теперь
   мониторинг и «Проверить сейчас» игнорируют прокси-настройки: агенты живут на приватных адресах
   туннеля. Регресс — `tests/server/test_client_http.py::TestProxyIsIgnored` (при `trust_env=True`
   тест падает).
3. **Поиск и уникальность — в Python (`casefold`), а не SQL `lower()`.** SQLite приводит регистр
   только для латиницы, поэтому «Магазин №5» и «магазин №5» через SQL считались бы разными: это
   ломало и проверку дубликата имени, и поиск по списку.
4. **Health Monitor выключен в тестовой сборке приложения**
   (`MAGAZ_HEALTH_MONITOR_ENABLED=false` в `tests/server/conftest.py`): фоновый опрос не должен
   ходить в сеть за спиной сценария. Там, где мониторинг проверяется, цикл запускается явно
   (`run_cycle`, `HealthMonitor.poll_once`) с транспортом `httpx.MockTransport`.
5. **`busy`/сессии/пути не кэшируются.** Они приходят только по кнопке «Проверить сейчас»:
   хранить их значило бы показывать устаревшие данные, поэтому карточка честно помечает, что это
   ответ агента на момент запроса.
6. **`POST /stores/{id}/check` — асинхронный эндпоинт** (httpx), поэтому сессию БД он открывает
   сам из `context.db` и коммитит результат; проверка прав администратора остаётся общей
   зависимостью.

### Что дальше (Э3 «Ядро 1С (перенос v2)»)

`client/agent/one_c.py` (перенос логики v2 без изменений), очередь и мьютекс `merge`/`update`,
локальные эндпоинты §5.1.1 (`/check-files`, `/run-merge`, `/check-merge`, `/run-update-db`,
`/check-update-db`, `/download-file`, `/send-message`), режим `--console`; 32 теста v2 переносятся
и должны стать зелёными.

