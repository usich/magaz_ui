@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
rem ---------------------------------------------------------------------------
rem  Установка агента Magaz UI на кассе (§14.2, §7.1) — AD-агностично (R9).
rem
rem  Использование (запуск от имени администратора):
rem      install_agent.bat                    -> работает из текущего каталога,
rem                                              где лежит дистрибутив (exe рядом со скриптом)
rem      install_agent.bat D:\MagazAgent      -> свой каталог дистрибутива
rem      install_agent.bat D:\MagazAgent --no-tray
rem          -> без TrayAgent в Планировщике (для машин, где трей не нужен;
rem             уведомления останутся только через msg.exe службы, §7.4)
rem
rem  Агент вычисляет пути от места запуска exe (I-14), поэтому exe, конфиг
rem  agent-config.json и файлы обновления должны лежать в одном каталоге —
rem  том, который указан TARGET (по умолчанию — каталог запуска этого скрипта).
rem  Скрипт ничего не «досочиняет»: конфиг агента берётся готовым —
rem  его выдают в карточке магазина (§11.2), там же лежат store_id и токен.
rem ---------------------------------------------------------------------------

rem Целевой каталог: по умолчанию — каталог, из которого запущен скрипт
rem (то есть где лежит дистрибутив), чтобы агент работал прямо с места запуска.
rem Первым аргументом можно указать другой каталог дистрибутива.
set "TARGET=%~1"
if "%TARGET%"=="" (
  set "TARGET=%~dp0"
  rem Убираем завершающий слеш. Для корня диска, где слеш обязателен, оставляем.
  rem !TARGET! — отложенное раскрытие: в блоке %TARGET% раскрывался бы при разборе,
  rem а не при выполнении, поэтому здесь нужен EnableDelayedExpansion.
  if /i not "!TARGET!"=="!TARGET:~0,2!\" set "TARGET=!TARGET:~0,-1!"
)
set "NO_TRAY="
if /i "%~2"=="--no-tray" set "NO_TRAY=1"

echo [1/4] Проверка дистрибутива в "%TARGET%" ...
if not exist "%TARGET%\magaz-agent.exe" (
  echo      ОШИБКА: не найден "%TARGET%\magaz-agent.exe".
  echo      Запустите скрипт из каталога, где лежит дистрибутив, или передайте
  echo      его каталог первым аргументом. Пример: install_agent.bat D:\agent-magaz-ui
  exit /b 1
)
if not exist "%TARGET%\MagazTrayAgent.exe" (
  echo      ПРЕДУПРЕЖДЕНИЕ: нет "%TARGET%\MagazTrayAgent.exe".
  echo      TrayAgent не будет зарегистрирован, эквивалентно --no-tray.
)
if not exist "%TARGET%\agent-config.json" (
  echo      ПРЕДУПРЕЖДЕНИЕ: нет "%TARGET%\agent-config.json".
  echo      Возьмите его в карточке магазина: store_id, server_url, agent_token, base_1c_path.
  echo      Положите рядом с magaz-agent.exe и перезапустите скрипт.
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
