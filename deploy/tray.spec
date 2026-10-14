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

#: Корень репозитория: спека лежит в `deploy\`, поэтому на уровень выше
REPO_ROOT = Path(SPECPATH).resolve().parents[1]  # noqa: F821 - задаёт PyInstaller

a = Analysis(  # noqa: F821 - задаёт PyInstaller
    [str(REPO_ROOT / "client" / "tray" / "__main__.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=[],
    # Иконка (`pystray`) и окна (`tkinter`) импортируются внутри функций — часть
    # подмодулей подгружается по именам, поэтому перечисляем их явно.
    hiddenimports=[
        "pystray",
        "pystray._win32",
        "PIL.Image",
        "PIL.ImageDraw",
        "tkinter",
        "tkinter.ttk",
    ],
    # Трею не нужны серверные и служебные зависимости: он ходит только на 127.0.0.1
    excludes=["uvicorn", "fastapi", "sqlalchemy", "alembic", "win32serviceutil", "matplotlib"],
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
