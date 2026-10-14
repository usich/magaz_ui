@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem ---------------------------------------------------------------------------
rem  Сборка релиза (§12 Э6, §14.1 п. 5): SPA + exe агента и трея.
rem
rem  Требования сборочной машины: Python 3.12 + `pip install -r requirements-build.txt`,
rem  Node.js LTS (только для сборки SPA — в проде Node не нужен).
rem
rem      build_release.bat            -> dist\release\ + frontend\dist
rem      build_release.bat --skip-ui  -> только exe (SPA уже собрана)
rem
rem  Результат:
rem      frontend\dist\               — статика, её отдаёт сервер (§5.2.1)
rem      dist\release\magaz-agent.exe, MagazTrayAgent.exe, *.bat, README_INSTALL.md
rem ---------------------------------------------------------------------------

set "REPO_ROOT=%~dp0.."
pushd "%REPO_ROOT%"

set "SKIP_UI="
if /i "%~1"=="--skip-ui" set "SKIP_UI=1"

if not defined SKIP_UI (
  echo [1/3] Сборка SPA (npm ci + npm run build) ...
  pushd frontend
  if not exist node_modules ( npm ci ) else ( echo      node_modules уже есть — пропускаем npm ci )
  call npm run build
  if errorlevel 1 ( echo ОШИБКА сборки SPA & popd & popd & exit /b 1 )
  popd
) else (
  echo [1/3] Сборка SPA пропущена (--skip-ui).
)

echo [2/3] Сборка exe агента и TrayAgent ...
.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm deploy\agent.spec
if errorlevel 1 ( echo ОШИБКА сборки magaz-agent.exe & popd & exit /b 1 )
.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm deploy\tray.spec
if errorlevel 1 ( echo ОШИБКА сборки MagazTrayAgent.exe & popd & exit /b 1 )

echo [3/3] Сборка дистрибутива dist\release ...
if not exist dist\release mkdir dist\release
copy /y dist\magaz-agent.exe dist\release\ >nul
copy /y dist\MagazTrayAgent.exe dist\release\ >nul
copy /y deploy\install_agent.bat dist\release\ >nul
copy /y deploy\uninstall_agent.bat dist\release\ >nul
copy /y deploy\README_INSTALL.md dist\release\ >nul

echo Готово. Дистрибутив кассы: dist\release\
echo Дальше: §14.2 — вписать agent-config.json из карточки магазина и запустить install_agent.bat
popd
endlocal
