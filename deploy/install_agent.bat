@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem ---------------------------------------------------------------------------
rem  Установка агента Magaz UI на кассе (§14.2, §7.1) — AD-агностично (R9).
rem
rem  Использование (запуск от имени администратора):
rem      install_agent.bat                    -> каталог по умолчанию C:\MagazAgent
rem      install_agent.bat D:\MagazAgent      -> свой каталог
rem      install_agent.bat C:\MagazAgent --no-tray
rem          -> без TrayAgent в Планировщике (для машин, где трей не нужен;
rem             уведомления останутся только через msg.exe службы, §7.4)
rem
rem  Скрипт ничего не «досочиняет»: конфиг агента берётся готовым —
rem  его выдают в карточке магазина (§11.2), там же лежат store_id и токен.
rem ---------------------------------------------------------------------------

set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=C:\MagazAgent"
set "NO_TRAY="
if /i "%~2"=="--no-tray" set "NO_TRAY=1"

if not exist "%TARGET%" mkdir "%TARGET%"

echo [1/4] Проверка файлов в "%TARGET%" ...
if not exist "%TARGET%\magaz-agent.exe" (
  echo      ОШИБКА: не найден "%TARGET%\magaz-agent.exe".
  echo      Скопируйте magaz-agent.exe, MagazTrayAgent.exe и agent-config.json
  echo      из дистрибутива в "%TARGET%" и запустите скрипт снова.
  exit /b 1
)
if not exist "%TARGET%\agent-config.json" (
  echo      ПРЕДУПРЕЖДЕНИЕ: нет agent-config.json — возьмите его в карточке
  echo      магазина (store_id, server_url, agent_token, base_1c_path).
)

echo [2/4] Установка службы MagazAgent ...
"%TARGET%\magaz-agent.exe" install --startup auto
if errorlevel 1 (
  echo      ОШИБКА: служба не установлена. Нужны права администратора.
  exit /b 1
)
"%TARGET%\magaz-agent.exe" start

echo [3/4] Регистрация TrayAgent в Планировщике ...
if defined NO_TRAY (
  echo      Пропущено (--no-tray).
  goto :check
)
rem Без домена задача ставится группе "Users", в домене — группе домена.
rem Имя группы локализовано, поэтому проверяем домен, а не «AD есть/нет» (R9).
set "TRAY_USER=Users"
if defined USERDOMAIN if /i not "%USERDOMAIN%"=="%COMPUTERNAME%" set "TRAY_USER=%USERDOMAIN%\Пользователи"
schtasks /create /f /tn MagazTrayAgent ^
  /tr "\"%TARGET%\MagazTrayAgent.exe\" --interval 5" ^
  /sc onlogon /ru "%TRAY_USER%" /rl highest
if errorlevel 1 echo      ПРЕДУПРЕЖДЕНИЕ: задача Планировщика не создана — заведите её вручную (docs\RUNBOOK.md).

:check
echo [4/4] Проверка: GET http://127.0.0.1:8383/ ...
"%TARGET%\magaz-agent.exe" info
echo.
echo Готово. Проверьте магазин в дашборде: он должен стать «онлайн» (§14.2).
echo Диагностика: "%TARGET%\magaz-agent.exe" --console
endlocal
