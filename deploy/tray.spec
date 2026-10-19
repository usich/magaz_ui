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

a = Analysis(  # noqa: F821 - задаёт PyInstaller
    [str(REPO_ROOT / "client" / "tray" / "__main__.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
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
