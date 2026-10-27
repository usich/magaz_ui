# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спек TrayAgent (§12 Э6, §7.4): `MagazTrayAgent.exe`.

Показывает уведомления в сессии пользователя: служба живёт в Session 0 и окон
не показывает (R3). Exe собирается **без консоли**: он запускается Планировщиком
при входе, и чёрное окно на экране кассира недопустимо. Поэтому логи трея идут в
файл `C:\\ProgramData\\MagazAgent\\logs\\tray.log` (`configure_tray_logging`,
параметр `interval` в bat-скрипте).

Сборка (со сборочной машины):

    pyinstaller --clean --noconfirm deploy\\tray.spec
"""

from pathlib import Path

#: Корень репозитория: `SPECPATH` — каталог спеки (`deploy\`), поэтому на уровень выше
#: (та же находка Э7, что и в `agent.spec`: `parents[1]` уходил выше репозитория).
REPO_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - задаёт PyInstaller

#: Модули Qt, которые подгружаются по именам: PyInstaller не видит их статически.
#: Интерфейс трея — PySide6 (спека 1.22): окна, значок `QSystemTrayIcon`, графика.
QT_HIDDEN_IMPORTS = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]

#: VC++ runtime DLL, без которых `Qt6Core.dll` не загружается на чистой кассе
#: («DLL load failed while importing QtCore»). PyInstaller не всегда включает их
#: сам, поэтому форсируем из каталога PySide6. Путь PySide6 ищем из окружения
#: сборочной машины, где ставится requirements-build.txt.
def _pyside6_runtime_binaries() -> list[tuple[str, str]]:
    import PySide6

    pyside_dir = Path(PySide6.__file__).resolve().parent
    names = (
        "msvcp140.dll",
        "msvcp140_1.dll",
        "msvcp140_2.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
    )
    return [(str(pyside_dir / name), ".") for name in names]


RUNTIME_BINARIES = _pyside6_runtime_binaries()

#: `Qt6Core.dll` импортирует `icuuc.dll` (и `icuin.dll`) — PySide6 их не
#: поставляет, а полагается на системную ICU Windows. На «чистой» кассе её
#: может не быть (или версия не та) → «DLL load failed while importing QtCore».
#: Поэтому включаем ICU из System32 сборочной машины в exe.
def _system_icu_binaries() -> list[tuple[str, str]]:
    system32 = Path("C:/Windows/System32")
    names = ("icuuc.dll", "icuin.dll")
    return [(str(system32 / name), ".") for name in names]


RUNTIME_BINARIES = _pyside6_runtime_binaries() + _system_icu_binaries()

a = Analysis(  # noqa: F821 - задаёт PyInstaller
    [str(REPO_ROOT / "client" / "tray" / "__main__.py")],
    pathex=[str(REPO_ROOT)],
    # VC++ runtime для Qt6Core.dll (иначе «DLL load failed while importing QtCore»)
    binaries=RUNTIME_BINARIES,
    datas=[],
    # Интерфейс и значок — Qt; часть подмодулей подгружается по именам, поэтому
    # перечисляем их явно (см. QT_HIDDEN_IMPORTS)
    hiddenimports=QT_HIDDEN_IMPORTS,
    # Трею не нужны серверные и служебные зависимости: он ходит только на 127.0.0.1.
    # Qt-модули, которых у трея нет (WebEngine, QML, 3D), исключаем: иначе exe
    # вырастает на сотни мегабайт, а кассе нужен только виджетный стек (§7.4)
    excludes=[
        "uvicorn",
        "fastapi",
        "sqlalchemy",
        "alembic",
        "win32serviceutil",
        "matplotlib",
        "tkinter",
        "pystray",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtNetworkAuth",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtBluetooth",
        "PySide6.QtDesigner",
        "PySide6.QtHelp",
        "PySide6.QtNfc",
        "PySide6.QtPdf",
        "PySide6.QtPositioning",
        "PySide6.QtRemoteObjects",
        "PySide6.QtScxml",
        "PySide6.QtSensors",
        "PySide6.QtSerialPort",
        "PySide6.QtSql",
        "PySide6.QtSvgWidgets",
        "PySide6.QtTest",
        "PySide6.QtWebChannel",
        "PySide6.QtWebSockets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821 - задаёт PyInstaller

exe = EXE(  # noqa: F821 - задаёт PyInstaller
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MagazTrayAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # окно трея: консоли быть не должно (см. докстроку)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
