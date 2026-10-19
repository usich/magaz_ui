"""Логика интерфейса трея на Qt (спека 1.22): геометрия, разметка, группировка.

Тесты не поднимают окон: проверяются чистые функции и связи между модулями —
позиция тоста в углу, помещается ли читалка на маленьком экране, как выглядит
строка списка при разных группировках и как собирается HTML тела. Так набор
остаётся быстрым и работает без графической сессии (сборки CI/касса — разные).

Отдельно проверяется деградация: `apply_dwm_glass` на «чужом» HWND возвращает
`False`, а не падает — приложение обязано работать и без DWM.
"""

from __future__ import annotations

from client.tray.presenter import GROUP_BY_CATEGORY, GROUP_BY_SEVERITY
from client.tray.richtext import KIND_IMAGE, body_blocks
from client.tray.ui import glass
from client.tray.ui.body import blocks_to_html, image_pixel_width, png_data_url
from client.tray.ui.panel import panel_mode_label, row_text
from client.tray.ui.reader import READER_MIN_HEIGHT, READER_MIN_WIDTH, reader_geometry
from client.tray.ui.theme import severity_color, severity_label
from client.tray.ui.toast import toast_geometry
from shared.enums import NotificationSeverity
from shared.schemas import NotificationCategoryRef, NotificationPendingItem


def make_item(
    notification_id: int = 1,
    *,
    severity: NotificationSeverity = NotificationSeverity.INFO,
    category: NotificationCategoryRef | None = None,
    body: str = "Сохраните смену",
    body_format: str = "plain",
) -> NotificationPendingItem:
    """Уведомление так, как его отдаёт служба на кассу (§5.1.2)."""
    return NotificationPendingItem(
        id=notification_id,
        title="Смена закрывается",
        body=body,
        body_format=body_format,
        severity=severity,
        category=category,
        requires_ack=True,
    )


class TestToastGeometry:
    """Тост — в правом верхнем углу, не выходит за края (§7.4, 1.22)."""

    def test_sits_in_top_right_corner(self) -> None:
        x, y, width, height = toast_geometry(1920, 1080)

        assert y == 16, "отступ сверху (§7.4)"
        assert x + width <= 1920
        assert x >= 1920 - width - 16
        assert width > 0 and height > 0

    def test_fits_narrow_screen(self) -> None:
        x, y, width, height = toast_geometry(800, 600)

        assert 0 <= x and x + width <= 800
        assert 0 <= y and y + height <= 600


class TestReaderGeometry:
    """Читалка помещается на экран и не становится нечитаемо узкой (§7.4, 1.22)."""

    def test_default_geometry_inside_screen(self) -> None:
        x, y, width, height = reader_geometry(1920, 1080)

        assert 0 <= x and x + width <= 1920
        assert 0 <= y and y + height <= 1080
        assert width >= READER_MIN_WIDTH
        assert height >= READER_MIN_HEIGHT

    def test_fits_small_screen(self) -> None:
        x, y, width, height = reader_geometry(800, 600)

        assert 0 <= x and x + width <= 800
        assert 0 <= y and y + height <= 600


class TestRowTextAndGroupLabels:
    """Плашка «противоположного» признака в строке списка (требование заказчика)."""

    def test_severity_group_shows_category(self) -> None:
        item = make_item(category=NotificationCategoryRef(id=3, name="Продажи"))

        text = row_text(item, by=GROUP_BY_SEVERITY)

        assert "Продажи" in text, "по важности — видно категорию"

    def test_severity_group_without_category(self) -> None:
        text = row_text(make_item(), by=GROUP_BY_SEVERITY)

        assert "без категории" in text

    def test_category_group_shows_severity(self) -> None:
        item = make_item(severity=NotificationSeverity.CRITICAL)

        text = row_text(item, by=GROUP_BY_CATEGORY)

        assert "критично" in text, "по категориям — видно важность"

    def test_mode_label(self) -> None:
        assert panel_mode_label(GROUP_BY_SEVERITY) == "по важности"
        assert panel_mode_label(GROUP_BY_CATEGORY) == "по категориям"


class TestSeverityColors:
    """Цвет плашки важности: красный — критично (требование заказчика, 1.22)."""

    def test_critical_is_red(self) -> None:
        assert severity_color(NotificationSeverity.CRITICAL) == "#dc2626"

    def test_labels(self) -> None:
        assert severity_label(NotificationSeverity.IMPORTANT) == "важно"


class TestGlassDegradation:
    """«Жидкое стекло» деградирует без падения: на «чужом» HWND стекла не будет."""

    def test_apply_on_bogus_handle_returns_bool(self) -> None:
        result = glass.apply_dwm_glass(0)

        assert isinstance(result, bool)
        assert result is False, "нулевой HWND — не окно, стекла не будет"

    def test_radius_constants_are_positive(self) -> None:
        assert glass.TOAST_RADIUS > 0
        assert glass.PANEL_RADIUS > 0


class TestImagePixelWidth:
    """Размер картинки из тела: его задаёт админ в редакторе (§11.5, Э9)."""

    def test_percent_counts_from_area(self) -> None:
        assert image_pixel_width("50%", 600) == 300
        assert image_pixel_width("100%", 600) == 600

    def test_pixels_are_limited_by_area(self) -> None:
        assert image_pixel_width("320px", 600) == 320
        assert image_pixel_width("900px", 600) == 600

    def test_too_small_width_is_raised_to_minimum(self) -> None:
        assert image_pixel_width("10px", 600) == 60
        assert image_pixel_width("5%", 600) == 60

    def test_empty_or_broken_width_takes_the_whole_area(self) -> None:
        assert image_pixel_width("", 600) == 600
        assert image_pixel_width("100vw", 600) == 600


class TestBlocksToHtml:
    """Разметка тела собирается в HTML: текст экранируется, картинки — метками."""

    def test_text_is_escaped(self) -> None:
        blocks = body_blocks("Опасно <b>тег</b>", "plain")

        html = blocks_to_html(blocks)

        assert "&lt;b&gt;" in html, "HTML из тела не должен «оживать»"

    def test_missing_image_shows_alt(self) -> None:
        blocks = body_blocks('<p>до <img src="magaz-image:plan.png" alt="схема"> после</p>', "html")

        html = blocks_to_html(blocks)

        assert "[схема]" in html, "нет картинки — остаётся подпись (§7.4)"

    def test_image_with_source_is_rendered(self) -> None:
        blocks = body_blocks(
            '<img src="magaz-image:plan.png" alt="схема" style="width:50%">', "html"
        )
        names = [block.name for block in blocks if block.kind == KIND_IMAGE]

        html = blocks_to_html(
            blocks,
            image_sources={names[0]: "data:image/png;base64,AAAA"},
            available_width=400,
        )

        assert "<img" in html and "base64" in html
        assert 'width="200"' in html, "50% от 400 px (§11.5)"

    def test_png_data_url_rejects_garbage(self) -> None:
        assert png_data_url(b"not an image") is None
