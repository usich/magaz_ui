# vendor_icu

Автономная **AMD64 ICU 74** для встраивания в `MagazTrayAgent.exe`.

## Зачем

`Qt6Core.dll` (PySide6 6.10) жёстко импортирует `icuuc.dll`, но не поставляет её —
PySide6 полагается на системную ICU Windows. На кассах без системной ICU
падает `DLL load failed while importing QtCore: Не найден указанный модуль`
(WinError 126).

## Источник

Официальный релиз ICU, `icu4c-74_2-Win64-MSVC2019.zip`:

    https://github.com/unicode-org/icu/releases/tag/release-74-2

* `icuuc.dll`  — переименован из `bin64/icuuc74.dll` (частый AMD64);
* `icudt74.dll` — `bin64/icudt74.dll` (данные ICU; требуется icuuc74).

Qt 6.10 совместима с ICU 74 (проверено: собранный exe запускает трей).

## Использование в tray.spec

`_icu_entries()` кладёт `icuuc.dll` (+`icudt74.dll`) в подпапку `PySide6\`
архива, где лежит `Qt6Core.dll` — Windows ищет зависимости DLL сначала в
каталоге самой DLL, поэтому Qt6Core находит icuuc рядом с собой.