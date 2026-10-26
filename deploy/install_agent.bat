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

rem Скрипт самодостаточен: недостающие файлы берутся из каталога, где лежит
rem этот bat (то есть из дистрибутива), и копируются в %TARGET%. Так дистрибутив
rem можно распаковать в любую папку и запустить install_agent.bat без ручного
rem копирования exe в целевой каталог.
set "SCRIPT_DIR=%~dp0"

echo [1/4] Развёртывание файлов в "%TARGET%" ...
if not exist "%TARGET%\magaz-agent.exe" (
  if exist "%SCRIPT_DIR%magaz-agent.exe" (
    copy /y "%SCRIPT_DIR%magaz-agent.exe" "%TARGET%\" >nul
    echo      Скопирован magaz-agent.exe из дистрибутива.
  ) else (
    echo      ОШИБКА: magaz-agent.exe нет ни в "%TARGET%", ни рядом со скриптом.
    echo      Каталог скрипта: %SCRIPT_DIR%
    echo      Положите magaz-agent.exe рядом со скриптом или в "%TARGET%" и запустите снова.
    exit /b 1
  )
)
if not exist "%TARGET%\MagazTrayAgent.exe" (
  if exist "%SCRIPT_DIR%MagazTrayAgent.exe" (
    copy /y "%SCRIPT_DIR%MagazTrayAgent.exe" "%TARGET%\" >nul
    echo      Скопирован MagazTrayAgent.exe из дистрибутива.
  ) else (
    echo      ПРЕДУПРЕЖДЕНИЕ: MagazTrayAgent.exe не найден ни в "%TARGET%", ни рядом
    echo      со скриптом. TrayAgent не будет зарегистрирован, эквивалентно --no-tray.
  )
)
if not exist "%TARGET%\agent-config.json" (
  if exist "%SCRIPT_DIR%agent-config.json" (
    copy /y "%SCRIPT_DIR%agent-config.json" "%TARGET%\" >nul
    echo      Скопирован agent-config.json из дистрибутива.
  ) else (
    echo      ПРЕДУПРЕЖДЕНИЕ: нет agent-config.json.
    echo      Возьмите его в карточке магазина: store_id, server_url, agent_token, base_1c_path.
    echo      Положите рядом со скриптом или в "%TARGET%", затем перезапустите скрипт.
  )
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
  echo      Пропущено: --no-tray.
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
