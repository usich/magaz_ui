# Миграции схемы сервера

Каталог Alembic (§6). Схема = модели §4 из `docs/PROJECT_SPEC.md`.

## Команды

```powershell
# из корня репозитория (venv активирован)
alembic upgrade head                 # применить миграции
alembic current                      # текущая ревизия
alembic revision --autogenerate -m "что изменено"   # новая миграция по моделям
alembic downgrade -1                 # откат последней (SQLite: batch-режим включён)
```

## Автоприменение при старте сервера

Сервер применяет ожидающие миграции сам: в `lifespan` (`server/main.py`) перед
проверкой схемы вызывается `server/db/migrations.run_migrations` — идемпотентный
`alembic upgrade head`. Это защищает от «no such column», когда к рабочей БД ещё
не подошла миграция. Управляется настройкой `MAGAZ_AUTO_MIGRATE` (по умолчанию
`true`; `false` — вернуть ручной шаг). В тестах автоприменение выключено
(`tests/server/conftest.py`): там схему создаёт `Base.metadata.create_all`.

URL для миграций при автоприменении передаётся через `magaz.url_override`
(`server/migrations/env.py`): `get_database_url` сначала смотрит его, затем — в
настройки приложения, поэтому автоприменение и CLI `alembic` ходят в одну БД.

Строка подключения берётся из `server/config.py` (`MAGAZ_DATABASE_URL`), поэтому
`alembic.ini` содержит только заглушку — правки в нём не нужны.

## Правила

1. Модель меняется **вместе** с миграцией: сначала правка `server/db/models.py`,
   затем `alembic revision --autogenerate`.
2. Проверка, что автогенерация не находит расхождений:

   ```powershell
   alembic revision --autogenerate -m "check"
   # если upgrade()/downgrade() пустые — модель и схема совпадают,
   # созданный файл удаляем
   ```

3. Для SQLite `render_as_batch=True` уже включён в `env.py`: изменение колонок
   выполняется пересозданием таблицы (иначе Alembic падает на ALTER).
