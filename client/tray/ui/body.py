"""Тело уведомления: разметка и картинки из локального кэша (спека 1.22, §4.6, §7.4).

Один вид на читалку и меню: раньше разметку рисовал `tkinter.Text` тегами, здесь
то же самое отдаётся `QTextBrowser` с HTML-фрагментами. Разбор тела уже есть в
`client.tray.richtext` (общий с прежней версией), поэтому окна не занимаются
парсингом: они получают блоки и раскладывают их.

Картинки берутся из локального кэша кассы (`TrayAgentClient.image`), а не из
сети: уведомление читается и без интернета (§7.4). Если картинки нет — в теле
остаётся подпись `alt`, окно не «ломается».
"""

from __future__ import annotations

import re
from collections.abc import Callable

from PySide6.QtCore import QBuffer, QByteArray, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QTextBrowser

from client.tray.richtext import (
    KIND_CODE,
    KIND_HEADING,
    KIND_IMAGE,
    KIND_QUOTE,
    KIND_RULE,
    Block,
    body_blocks,
)
from client.tray.ui.theme import TEXT, TEXT_MUTED
from shared.schemas import NotificationPendingItem

__all__ = ["BodyView", "blocks_to_html", "image_pixel_width", "png_data_url"]

#: Ограничения ширины картинки: как в прежней версии (§11.5, Э9)
IMAGE_MAX_WIDTH = 900
IMAGE_MIN_WIDTH = 60


def image_pixel_width(width: str, available: int) -> int:
    """Ширина картинки в пикселях: админ задал её в редакторе (§11.5, Э9).

    Логика та же, что в прежнем `client.tray.__main__._image_pixel_width`:
    процент считается от ширины области, пиксели берутся как есть, но не больше
    области и не меньше `IMAGE_MIN_WIDTH`. Пустая ширина — «по ширине окна».
    """
    match = re.match(r"^(\d{1,4})(px|%)$", str(width or "").strip().lower())
    if match is None:
        return available
    value = int(match.group(1))
    if match.group(2) == "px":
        return max(IMAGE_MIN_WIDTH, min(available, value))
    return max(IMAGE_MIN_WIDTH, min(available, int(available * value / 100)))


def _escaped(text: str) -> str:
    """Экранировать текст: тело приходит от админа, HTML не должен «оживать»."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _runs_html(block: Block) -> str:
    """Собрать фрагмент блока: жирный, курсив, код, цвет и фон (Э9, §4.6)."""
    parts: list[str] = []
    for run in block.runs:
        piece = _escaped(run.text)
        if run.code:
            piece = f"<code>{piece}</code>"
        if run.bold:
            piece = f"<b>{piece}</b>"
        if run.italic:
            piece = f"<i>{piece}</i>"
        if run.underline:
            piece = f"<u>{piece}</u>"
        if run.strike:
            piece = f"<s>{piece}</s>"
        styles: list[str] = []
        if run.color:
            styles.append(f"color:{run.color}")
        if run.background:
            styles.append(f"background-color:{run.background}")
        if styles:
            piece = f'<span style="{"; ".join(styles)}">{piece}</span>'
        parts.append(piece)
    return "".join(parts)


def blocks_to_html(
    blocks: list[Block],
    *,
    image_sources: dict[str, str] | None = None,
    available_width: int = 520,
) -> str:
    """Разметка тела в HTML для `QTextBrowser` (спека 1.22, Э9).

    `image_sources` — готовые адреса картинок (локальный кэш кассы): имя из метки
    `magaz-image:<имя>` → `data:`-URL. Нет адреса — остаётся подпись, как в
    прежней версии.
    """
    pieces: list[str] = []
    sources = image_sources or {}
    for block in blocks:
        if block.kind == KIND_RULE:
            pieces.append(f'<p style="color:{TEXT_MUTED}">────────</p>')
            continue
        if block.kind == KIND_IMAGE:
            alt = _escaped(block.alt or block.name)
            source = sources.get(str(block.name))
            if source:
                pixels = image_pixel_width(block.width, available_width)
                pieces.append(f'<p><img src="{source}" width="{pixels}" alt="{alt}"></p>')
            else:
                pieces.append(f'<p style="color:{TEXT_MUTED}">[{alt}]</p>')
            continue
        marker = f"{_escaped(block.marker)} " if block.marker else ""
        content = _runs_html(block)
        if block.kind == KIND_HEADING:
            pieces.append(f"<p><b>{marker}{content}</b></p>")
        elif block.kind == KIND_QUOTE:
            pieces.append(
                f'<p style="color:{TEXT_MUTED}; margin-left:12px">{marker}{content}</p>'
            )
        elif block.kind == KIND_CODE:
            pieces.append(f"<p><code>{marker}{content}</code></p>")
        else:
            pieces.append(f"<p>{marker}{content}</p>")
    return "".join(pieces) or f'<p style="color:{TEXT_MUTED}">Пустое сообщение</p>'


def png_data_url(data: bytes) -> str | None:
    """`data:`-URL из байтов картинки — Qt-браузер покажет её без файла (§7.4)."""
    image = QImage()
    if not image.loadFromData(QByteArray(data)):
        return None
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        return None
    encoded = bytes(buffer.data().toBase64()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


class BodyView(QTextBrowser):
    """Тело уведомления: разметка, картинки, только чтение (спека 1.22)."""

    def __init__(
        self,
        item: NotificationPendingItem,
        *,
        image_provider: Callable[[int, str], bytes | None] | None = None,
    ) -> None:
        super().__init__()
        self.item = item
        self._provider = image_provider
        self.setOpenExternalLinks(True)
        self.setFrameShape(QTextBrowser.Shape.NoFrame)
        self.setStyleSheet(f"QTextBrowser {{ background: transparent; color: {TEXT}; }}")
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.render_item(item)

    def render_item(self, item: NotificationPendingItem) -> None:
        """Показать уведомление: собрать разметку и подставить картинки из кэша."""
        self.item = item
        blocks = body_blocks(item.body, item.body_format)
        sources: dict[str, str] = {}
        if self._provider is not None:
            for block in blocks:
                if block.kind != KIND_IMAGE or str(block.name) in sources:
                    continue
                data = self._provider(item.id, str(block.name))
                if not data:
                    continue
                url = png_data_url(data)
                if url:
                    sources[str(block.name)] = url
        available = max(200, int(self.width()) - 40)
        html = blocks_to_html(blocks, image_sources=sources, available_width=available)
        self.setHtml(html)

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt
        """Перерисовать: ширина области меняется — проценты картинок считаются заново."""
        super().resizeEvent(event)
        self.render_item(self.item)
