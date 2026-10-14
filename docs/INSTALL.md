# Установка Magaz UI (Э6, §14)

Документ описывает развёртывание **с чистого Windows** и рассчитан на ИТ-специалиста:
ни одна строка ниже не требует правки кода (§12, Э6). Эксплуатация, диагностика и
восстановление — `docs\RUNBOOK.md`; краткая памятка для кассы — `deploy\README_INSTALL.md`.

Что где находится:

| Компонент | Каталог | Кто запускает |
|---|---|---|
| Сервер дашборда (FastAPI + SPA) | любой каталог репозитория/поставки | `python -m server.main` или `deploy\run_server.bat` |
| Служба кассы `MagazAgent` | `C:\MagazAgent\` | диспетчер служб Windows (Session 0) |
| Показ уведомлений `MagazTrayAgent` | `C:\MagazAgent\` | Планировщик, при входе пользователя (§7.4, R3) |

Требования: Windows 10/11 или Windows Server 2019+; Python 3.12 на сервере и сборочной
машине; Node.js LTS — **только** для сборки SPA (в проде Node не нужен, §14.1 п. 5);
на кассе ничего, кроме exe, не требуется.

---

## 1. Сборка поставки (один раз, на сборочной машине)

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
deploy\build_release.bat
```

Скрипт делает три вещи (§12 Э6):

1. собирает SPA: `npm ci && npm run build` → `frontend\dist` (эту папку отдаёт сервер, §5.2.1);
2. собирает exe по специям: `deploy\agent.spec` → `magaz-agent.exe`, `deploy\tray.spec`
   → `MagazTrayAgent.exe` (без консоли: на экране кассира не должно быть чёрного окна);
3. складывает дистрибутив кассы в `dist\release\` — exe, `install_agent.bat`,
   `uninstall_agent.bat`, `README_INSTALL.md`.

`pywin32` попадает **внутрь** exe агента, поэтому на кассе его ставить не нужно.
Флаг `deploy\build_release.bat --skip-ui` пропускает сборку SPA (полезно при
повторной сборке exe).

---

## 2. Сервер дашборда

### 2.1 Первый запуск

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m server.cli create-admin --username admin
.venv\Scripts\python.exe -m server.cli info
deploy\run_server.bat
```

* `alembic upgrade head` создаёт `server\data\magaz.db` и применяет миграции (§4);
* `create-admin` спросит пароль интерактивно — так он не остаётся в истории команд;
* `info` показывает версию, путь к БД, состояние `frontend\dist` и адрес для агентов;
* SPA отдаётся сервером: `GET /` открывает дашборд, `GET /api/docs` — OpenAPI (§5.2.1).

### 2.2 Настройки

Значения читаются из переменных окружения `MAGAZ_*` и/или `.env` в корне
(`server/config.py`, §14.1 п. 4). Основные:

| Переменная | Значение по умолчанию | Зачем |
|---|---|---|
| `MAGAZ_HOST`, `MAGAZ_PORT` | `0.0.0.0`, `8080` | адрес и порт дашборда |
| `MAGAZ_DATABASE_URL` | `sqlite:///server/data/magaz.db` | SQLite; для PostgreSQL — своя строка |
| `MAGAZ_PUBLIC_SERVER_URL` | пусто | адрес, который попадает в блок конфига агента (§11.2) |
| `MAGAZ_COOKIE_SECURE` | `false` | `true` при работе через HTTPS-туннель (§8.2) |
| `MAGAZ_SERVE_SPA` | `true` | отдавать SPA из `frontend\dist` |
| `MAGAZ_AGENT_SCHEME` | `http` | схема обращения к агентам внутри туннеля |

Настройки мониторинга, операций, оборудования и уведомлений (интервалы, пороги,
`notification_snooze_min`) меняются в дашборде на странице `/settings` (§4.9) —
перезапуск сервера для них не нужен.

### 2.3 Секреты

При первом запуске создаются два файла в `server\data\`:

* `secrets.key` — ключ Fernet для паролей 1С (П-1);
* `cookie_secret.key` — ключ подписи сессионных cookie.

Права на `server\data\` — только у администратора сервера; в бэкап БД эти файлы
**не входят** (R15): без `secrets.key` сохранённые пароли 1С не расшифровать, но и
утечка бэкапа БД не даёт доступа к учётным записям 1С. Порядок бэкапа и
восстановления — `docs\RUNBOOK.md`.

### 2.4 Сервер как служба (на усмотрение ИТ, §14.1 п. 3)

Собственный exe-сервис в этой итерации не поставляется: сервер можно поднять любым
привычным способом. Два проверенных варианта:

1. **Планировщик заданий** (просто, без сторонних утилит):

   ```bat
   schtasks /create /f /tn MagazUI-Server /sc onstart /ru "СИСТЕМА" ^
     /tr "\"C:\MagazUI\.venv\Scripts\python.exe\" -m server.main"
   ```

   Перед этим запустите сервер вручную и убедитесь, что БД и `frontend\dist` на месте.
2. **NSSM** (если нужен сервис в `services.msc`): установите NSSM и выполните
   `nssm install MagazUI "C:\MagazUI\.venv\Scripts\python.exe" -m server.main`,
   затем `nssm start MagazUI`.

В обоих случаях рабочая папка процесса — корень поставки: относительные пути к
`server\data` и `frontend\dist` вычисляются от него.


---

## 3. Касса: агент и трей (§14.2)

Порядок из пяти шагов (полная памятка с командами диагностики — `deploy\README_INSTALL.md`):

1. Скопировать `magaz-agent.exe`, `MagazTrayAgent.exe` и `install_agent.bat` в
   `C:\MagazAgent\` — не в `Program Files`: там же лежат `.cf`, `.cfe`, `.xml`, конфиг,
   состояние и `notifications.json` (§7.1).
2. Положить рядом `agent-config.json`, который выдали в карточке магазина: в нём
   `store_id`, `server_url`, `agent_token`, `base_1c_path`, профиль 1С. Токен и пароль
   в UI не показываются (§8.1) — вписывать их руками не нужно.
3. Запустить от имени администратора:

   ```bat
   cd /d C:\MagazAgent
   install_agent.bat
   ```

   Скрипт: проверит наличие файлов → `magaz-agent.exe install --startup auto` →
   `start` → зарегистрирует `MagazTrayAgent` в Планировщике на вход в систему
   (AD-агностично, R9: без домена — группа `Users`, в домене — группа домена;
   флаг `--no-tray` отключает трей, тогда остаётся только дублирующий канал
   `msg.exe`, §7.4) → выведет `info` для самопроверки.
4. Проверить в дашборде: магазин стал «онлайн», в карточке видны версия агента, ОС,
   рабочая папка и `base_1c_path` (кнопка «Проверить сейчас», Д-3).
5. Положить файлы обновления в `C:\MagazAgent\` и нажать «Проверить файлы»: матрица
   `магазин × (cf/cfe/xml)` заполнится (§11.3).

Проверка после перезагрузки: служба стартует автоматически, поэтому магазин снова
становится «онлайн» без входа пользователя — это критерий приёмки Э6, он проверяется
на кассе (Э7, Приложение A).

---

## 4. Обновление и удаление

**Агент (первая итерация — вручную, §14.3).** Автообновление — Э8, поэтому порядок
такой: `magaz-agent.exe stop` → заменить `magaz-agent.exe` и `MagazTrayAgent.exe` →
`magaz-agent.exe start` → `schtasks /run /tn MagazTrayAgent` (или перелогиниться).
Конфиг, `notifications.json` и файлы обновления при этом не трогаются.

**Удаление.** `uninstall_agent.bat` снимает службу и задачу, данные оставляет;
`uninstall_agent.bat C:\MagazAgent --purge` удаляет и каталог (перед этим сохраните
`notifications.json`, если он нужен для отчёта, — `docs\RUNBOOK.md`).

**Сервер.** Остановить процесс → скопировать `server\data` целиком (`magaz.db`,
`secrets.key`, `cookie_secret.key`) → заменить код и `frontend\dist` → запустить →
`alembic upgrade head`, если в поставке есть новые миграции.

---

## 5. Чеклист приёмки Э6 (проверяется на стенде, Э7)

- [ ] `deploy\build_release.bat` собирает `dist\release\` без ошибок; в `frontend\dist`
      лежит собранная SPA;
- [ ] сервер поднимается на чистой машине по §2 без правок кода, дашборд открывается
      из собранного SPA (`GET /` → экран входа);
- [ ] `install_agent.bat` на чистой кассе ставит службу и задачу Планировщика;
      магазин появляется в дашборде как «онлайн»;
- [ ] **после перезагрузки** кассы служба стартует сама, магазин снова «онлайн»
      без входа пользователя;
- [ ] при входе пользователя поднимается TrayAgent: уведомление `important`
      показывается окном с кнопками «Ознакомлен» и «Отложить», отложенное
      возвращается через `notification_snooze_min` (П-2) и **не** считается
      ознакомленным;
- [ ] `magaz-agent.exe info`, `magaz-agent.exe --console`,
      `MagazTrayAgent.exe --pending` дают понятный вывод на кассе (диагностика
      §7.5, §14.4);
- [ ] логи агента и трея пишутся в `C:\ProgramData\MagazAgent\logs\` и ротируются;
- [ ] удаление (`uninstall_agent.bat`) снимает службу и задачу, данные остаются.

Пункты, которые **нельзя** проверить на машине разработки (нет 1С и нет сети кассы),
перечислены в §13.3 спецификации и выполняются на стенде заказчика.
