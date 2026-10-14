# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спек агента кассы (§12 Э6, §14.2): `magaz-agent.exe`.

Собирается **одним файлом**: на кассе это `C:\\MagazAgent\\magaz-agent.exe` рядом
с `agent-config.json`. Рабочая папка, состояние и логи вычисляются от места
запуска exe (`get_application_root()`, §7.1, I-14), поэтому в спеке нет
абсолютных путей и переносить папку можно целиком.

`console=True` — обязательное требование §7.5: `magaz-agent.exe --console` и
`magaz-agent.exe info` должны что-то печатать, иначе на кассе нечего смотреть.
Служба Windows при этом работает как обычно: консоль у неё просто не показывается.

Сборка (со сборочной машины):

    pyinstaller --clean --noconfirm deploy\\agent.spec
"""

from pathlib import Path

#: Корень репозитория: `SPECPATH` — каталог спеки (`deploy\`), поэтому на уровень выше.
#: Находка Э7: здесь стоял `parents[1]` — на уровень **выше** репозитория, и сборка падала
#: с «script 'C:\...\PycharmProjects\client\agent\__main__.py' not found».
REPO_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - задаёт PyInstaller

a = Analysis(  # noqa: F821 - задаёт PyInstaller
    [str(REPO_ROOT / "client" / "agent" / "__main__.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=[],
    # uvicorn и pywin32 подгружают часть модулей по именам строкой — статический
    # анализ их не видит, поэтому перечисляем явно (частая причина «exe стартует
    # и падает» на кассе).
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        "win32serviceutil",
        "win32service",
        "win32event",
        "servicemanager",
        "win32timezone",
    ],
    # Трею — свой exe (`tray.spec`): в службе ни иконки, ни GUI не нужны
    excludes=["tkinter", "pystray", "PIL", "matplotlib"],
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
    name="magaz-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX ломает подписи и антивирусные проверки на кассах
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
