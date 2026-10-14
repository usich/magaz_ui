@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem ---------------------------------------------------------------------------
rem  Запуск сервера дашборда (§14.1) из виртуального окружения репозитория.
rem
rem      run_server.bat            -> 0.0.0.0:8080 (значения по умолчанию)
rem      run_server.bat 8443       -> свой порт
rem
rem  Перед первым запуском: alembic upgrade head и создание администратора (§14.1).
rem  SPA отдаётся из frontend\dist: `cd frontend && npm ci && npm run build`.
rem  Автозапуск при перезагрузке: docs\INSTALL.md, раздел «Сервер как служба».
rem ---------------------------------------------------------------------------

set "REPO_ROOT=%~dp0.."
pushd "%REPO_ROOT%"

if not exist ".venv\Scripts\python.exe" (
  echo ОШИБКА: нет .venv. Создайте окружение: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  popd & exit /b 1
)

set "MAGAZ_PORT=%~1"
if not "%MAGAZ_PORT%"=="" set "MAGAZ_PORT=%MAGAZ_PORT%"

echo Запуск сервера Magaz UI (%MAGAZ_PORT% или порт из .env) ...
.venv\Scripts\python.exe -m server.main
popd
endlocal
