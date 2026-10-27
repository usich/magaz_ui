# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спек TrayAgent: `MagazTrayAgent.exe`.

Показывает уведомления в сессии пользователя: служба живёт в Session 0 и окон
не показывает. Exe собирается без консоли: он запускается Планировщиком при
входе, и чёрное окно на экране кассира недопустимо. Поэтому логи трея идут в
файл C:\ProgramData\MagazAgent\logs\tray.log.

Сборка (со сборочной машины):

    pyinstaller --clean --noconfirm deploy\tray.spec
"""

from pathlib import Path

REPO_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - задаёт PyInstaller

QT_HIDDEN_IMPORTS = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]


#: VC++ runtime DLL, без которых Qt6Core.dll не загружается на чистой кассе.
#: PyInstaller не всегда включает их сам, поэтому форсируем из каталога PySide6.
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


def _pe_machine(path: Path) -> int | None:
    """Читает поле Machine из PE-заголовка: 0x8664=AMD64, 0xAA64=ARM64, 0x14c=x86."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    pe = data.find(b"PE\x00\x00")
    if pe < 0 or pe + 6 > len(data):
        return None
    return data[pe + 4] | (data[pe + 5] << 8)


#: Qt6Core.dll жёстко импортирует icuuc.dll. PySide6 её не поставляет и полагается
#: на системную ICU Windows. Без неё на кассе падает
#: «DLL load failed while importing QtCore: Не найден указанный модуль» (WinErr 126).
#: Кладём ICU в подпапку PySide6\: Windows ищет зависимости DLL сначала в каталоге
#: самой DLL, и Qt6Core найдёт icuuc рядом с собой (_MEIxxx\PySide6\).
#:
#: Что берём:
#:   * если системная System32 ICU той же разрядности, что Qt (обычная x64-сборка),
#:     берём её (icuuc.dll + icuin.dll);
#:   * если System32 ICU несовместима (сборочная машина под Windows on ARM даёт
#:     ARM64) — включаем автономную AMD64 ICU 74 из deploy/vendor_icu/.
#:
#: ICU кладём в `datas`, а не `binaries`: PyInstaller отбрасывает из `binaries`
#: системные DLL (icuuc.dll есть в System32), а `datas` копирует как есть и в
#: onefile попадают в _MEI.
def _icu_entries() -> list[tuple[str, str]]:
    try:
        import PySide6
    except Exception:
        return []
    pyside_dir = Path(PySide6.__file__).resolve().parent
    qt_machine = _pe_machine(pyside_dir / "Qt6Core.dll")
    if qt_machine is None:
        return []
    system32 = Path("C:/Windows/System32")
    if any(_pe_machine(system32 / n) == qt_machine
           for n in ("icuuc.dll", "icuin.dll")):
        print("[_icu_entries] system ICU matches Qt bitness; bundling")
        out = [(str(system32 / n), "PySide6")
               for n in ("icuuc.dll", "icuin.dll")
               if (system32 / n).exists()]
        if out:
            return out
    vendor = Path(SPECPATH).joinpath("vendor_icu")  # noqa: F821
    if vendor.joinpath("icuuc.dll").exists():
        print(f"[_icu_entries] system ICU incompatible; bundling vendor ICU "
              f"from {vendor}")
        out = [(str(vendor / "icuuc.dll"), "PySide6")]
        if (vendor / "icudt74.dll").exists():
            out.append((str(vendor / "icudt74.dll"), "PySide6"))
        return out
    return []


RUNTIME_BINARIES = _pyside6_runtime_binaries()
ICU_DATAS = _icu_entries()
a = Analysis(  # noqa: F821 - задаёт PyInstaller
    [str(REPO_ROOT / "client" / "tray" / "__main__.py")],
    pathex=[str(REPO_ROOT)],
    # VC++ runtime для Qt6Core.dll (иначе «DLL load failed while importing QtCore»)
    binaries=RUNTIME_BINARIES,
    datas=ICU_DATAS,
    hiddenimports=QT_HIDDEN_IMPORTS,
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
    console=False,  # окно трея: консоль не нужна (см. докстроку)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)