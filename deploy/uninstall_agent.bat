@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem ---------------------------------------------------------------------------
rem  Удаление агента Magaz UI с кассы (§14.2, §14.3).
rem
rem      uninstall_agent.bat                  -> снять службу и задачу, данные оставить
rem      uninstall_agent.bat C:\MagazAgent     -> свой каталог
rem      uninstall_agent.bat C:\MagazAgent --purge
rem          -> дополнительно удалить каталог (agent-config.json, *.cf, *.cfe, *.xml,
rem             notifications.json) — файлы обновления будут потеряны!
rem
rem  Очередь уведомлений и состояние операций удаляются вместе с каталогом:
rem  перед чисткой при необходимости сохраните notifications.json (docs\RUNBOOK.md).
rem ---------------------------------------------------------------------------

set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=C:\MagazAgent"
set "PURGE="
if /i "%~2"=="--purge" set "PURGE=1"

echo [1/3] Остановка и удаление службы MagazAgent ...
if exist "%TARGET%\magaz-agent.exe" (
  "%TARGET%\magaz-agent.exe" stop
  "%TARGET%\magaz-agent.exe" remove
) else (
  echo      exe не найден — снимаем службу стандартной командой
  sc stop MagazAgent >nul 2>&1
  sc delete MagazAgent >nul 2>&1
)

echo [2/3] Удаление задачи Планировщика MagazTrayAgent ...
schtasks /end /tn MagazTrayAgent >nul 2>&1
schtasks /delete /f /tn MagazTrayAgent >nul 2>&1
taskkill /f /im MagazTrayAgent.exe >nul 2>&1

echo [3/3] Каталог "%TARGET%" ...
if defined PURGE (
  if exist "%TARGET%" rmdir /s /q "%TARGET%"
  echo      Удалён.
) else (
  echo      Оставлен (данные и логи не тронуты). Полное удаление: --purge
)
echo Готово. Магазин в дашборде станет «офлайн» после окна online_window_sec (§3.3).
endlocal
