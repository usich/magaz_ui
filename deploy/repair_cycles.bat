@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
rem ---------------------------------------------------------------------------
rem  Д-32: привязать уже созданные проверки файлов к циклу обновления.
rem
rem  Диспетчер до Д-32 ставил повторную проверку после скачивания без
rem  cycle_id/batch_id, поэтому в таблице «Магазины и статусы обновления»
rem  колонка «Проверка» показывала «—». Скрипт берёт задачи check_files без
rem  цикла и привязывает их к циклу обновления магазина: по родительской
rem  задаче скачивания, иначе к открытому циклу, иначе создаёт новый.
rem  Повторный запуск ничего не меняет — в выборку попадают только задачи
rem  с пустым cycle_id.
rem
rem  ЗАПУСКАТЬ НА СЕРВЕРЕ ДАШБОРДА: SQLite в режиме WAL по сети не
rem  поддерживается — database is locked, disk I/O error и риск порчи файла.
rem  Подробности: docs\RUNBOOK.md, раздел 4.
rem
rem  Использование:
rem      repair_cycles.bat               -> бэкап БД + остановка службы + ПЛАН,
rem                                         в БД ничего не пишется
rem      repair_cycles.bat apply         -> бэкап + остановка + запись + запуск
rem      repair_cycles.bat apply nosvc   -> то же, но службу не останавливать,
rem                                         если сервер запускается вручную
rem
rem  Переменные:
rem      SERVICE  имя службы сервера, по умолчанию MagazUI — NSSM, INSTALL.md §2.4
rem ---------------------------------------------------------------------------

set "APPLY="
set "NO_SVC="
if /i "%~1"=="apply" set "APPLY=--apply"
if /i "%~1"=="nosvc" set "NO_SVC=1"
if /i "%~2"=="apply" set "APPLY=--apply"
if /i "%~2"=="nosvc" set "NO_SVC=1"
if /i "%~3"=="apply" set "APPLY=--apply"
if /i "%~3"=="nosvc" set "NO_SVC=1"
if not defined SERVICE set "SERVICE=MagazUI"
rem Вывод Python в UTF-8: консоль переключена в 65001, иначе кириллица ломается
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

pushd "%~dp0.."

echo [1/5] Проверка окружения ...
if not exist ".venv\Scripts\python.exe" (
  echo      ОШИБКА: нет .venv - запускайте скрипт из папки deploy репозитория.
  popd
  exit /b 1
)
if not exist "deploy\backup_db.py" (
  echo      ОШИБКА: нет deploy\backup_db.py рядом со скриптом.
  popd
  exit /b 1
)

echo [2/5] Остановка службы "%SERVICE%" ...
if defined NO_SVC (
  echo      Пропущено: ключ nosvc. Сервер должен быть остановлен вручную.
) else (
  sc query "%SERVICE%" >nul 2>&1
  if errorlevel 1 (
    echo      Служба "%SERVICE%" не найдена - работаю без остановки сервера.
    echo      Если сервер запущен вручную, остановите его до продолжения.
    set "NO_SVC=1"
  ) else (
    sc query "%SERVICE%" | findstr /i "RUNNING" >nul
    if errorlevel 1 (
      echo      Служба уже остановлена.
    ) else (
      sc stop "%SERVICE%" >nul
      call :wait_state STOPPED 60
      if errorlevel 1 (
        echo      ОШИБКА: служба не остановилась за 2 минуты.
        popd
        exit /b 1
      )
      echo      Остановлена.
    )
  )
)

echo [3/5] Резервная копия БД ...
.venv\Scripts\python.exe "deploy\backup_db.py" --suffix=-before-repair
if errorlevel 1 (
  echo      ОШИБКА: без резервной копии ремонт не начинаем.
  call :start_service
  popd
  exit /b 1
)

echo [4/5] План: какие проверки будут привязаны ...
.venv\Scripts\python.exe -m server.cli repair-cycles
if errorlevel 1 (
  echo      ОШИБКА: план не построен.
  call :start_service
  popd
  exit /b 1
)

echo [5/5] Запись изменений ...
if not defined APPLY (
  echo      Пропущено: режим просмотра. Для записи: repair_cycles.bat apply
  call :start_service
  echo.
  echo Готово. В БД ничего не менялось.
  goto :done
)
.venv\Scripts\python.exe -m server.cli repair-cycles --apply
if errorlevel 1 (
  echo      ОШИБКА: запись не выполнена.
  call :start_service
  popd
  exit /b 1
)
echo.
echo Контроль: повторный план должен показать «Проверок без цикла: 0»
.venv\Scripts\python.exe -m server.cli repair-cycles
call :start_service
echo.
echo Готово. Проверьте в дашборде колонку «Проверка» у магазинов из списка.
goto :done

:wait_state
rem %1 - состояние RUNNING/STOPPED, %2 - число попыток по 2 секунды
set "WANT=%~1"
set /a LEFT=%~2
:wait_loop
sc query "%SERVICE%" | findstr /i "!WANT!" >nul
if not errorlevel 1 exit /b 0
set /a LEFT-=1
if !LEFT! leq 0 exit /b 1
ping -n 3 127.0.0.1 >nul
goto :wait_loop

:start_service
if defined NO_SVC (
  echo [Служба] Запуск не требуется - сервер запускается вручную.
  exit /b 0
)
sc query "%SERVICE%" | findstr /i "RUNNING" >nul
if not errorlevel 1 (
  echo [Служба] "%SERVICE%" уже работает.
  exit /b 0
)
echo [Служба] Запуск "%SERVICE%" ...
sc start "%SERVICE%" >nul
call :wait_state RUNNING 60
if errorlevel 1 echo      ПРЕДУПРЕЖДЕНИЕ: служба не запустилась за 2 минуты - проверьте вручную.
if not errorlevel 1 echo      Работает.
exit /b 0

:done
popd
rem Пауза только в интерактивной сессии: у задач Планировщика SESSIONNAME не задан
if defined SESSIONNAME pause
endlocal
