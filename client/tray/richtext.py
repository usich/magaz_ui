"""Разметка уведомления для окна кассы (§4.6, §7.4, Э9).

tkinter не умеет HTML, поэтому тело разбирается в блоки и фрагменты: окно рисует
текст тегами (`tkinter.Text` + `tag_configure`) и картинки из локального кэша.
Разметку уже проверил сервер (`shared.richtext`), задача этого модуля — **показать**
её, а не фильтровать: неизвестный тег просто становится обычным текстом, опасный
до кассы не доезжает.

Здесь нет ни `tkinter`, ни `PIL` — поэтому разбор разметки проверяется тестами
(§13), а не «на живой кассе».
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser

__all__ = [
    "KIND_CODE",
    "KIND_HEADING",
    "KIND_IMAGE",
    "KIND_ITEM",
    "KIND_PARAGRAPH",
    "KIND_QUOTE",
    "KIND_RULE",
    "Block",
    "TextRun",
    "body_blocks",
    "outline",
]

KIND_PARAGRAPH = "paragraph"
KIND_HEADING = "heading"
KIND_ITEM = "item"
KIND_QUOTE = "quote"
KIND_CODE = "code"
KIND_IMAGE = "image"
KIND_RULE = "rule"

#: Префикс метки картинки — тот же, что у сервера (`shared.richtext`)
IMAGE_SRC_PREFIX = "magaz-image:"

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}
#: Теги, которым соответствует «пустая строка»: блок закрывается, текст продолжается
_BLOCK_STARTS = frozenset({"p", "div", "blockquote", "pre", "li", "tr", "section"})
#: Теги выделения → признаки фрагмента
_MARKS: dict[str, str] = {
    "b": "bold",
    "strong": "bold",
    "i": "italic",
    "em": "italic",
    "u": "underline",
    "ins": "underline",
    "s": "strike",
    "strike": "strike",
    "del": "strike",
    "code": "code",
}
_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$|^[a-zA-Z]{3,20}$")


@dataclass
class TextRun:
    """Кусок текста с одинаковым оформлением — единица рисования в окне."""

    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    code: bool = False
    color: str | None = None
    background: str | None = None


@dataclass
class Block:
    """Абзац, заголовок, пункт списка, цитата, код, картинка или линия."""

    kind: str
    runs: list[TextRun] = field(default_factory=list)
    level: int = 0
    marker: str = ""
    name: str = ""
    alt: str = ""

    @property
    def text(self) -> str:
        """Склейка фрагментов — для подписей, поиска и тестов."""
        return "".join(run.text for run in self.runs)

    @property
    def is_empty(self) -> bool:
        """Пустой блок рисовать незачем."""
        return self.kind not in {KIND_IMAGE, KIND_RULE} and not self.text.strip()


class _BodyParser(HTMLParser):
    """Собрать тело в блоки: заголовки, списки, цитаты, картинки (§4.6, Э9)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._current: Block | None = None
        self._marks: list[str] = []
        self._color: str | None = None
        self._background: str | None = None
        #: Стек списков: `ordered` (нумерация) и счётчик пунктов
        self._lists: list[dict] = []

    # --- Блоки --------------------------------------------------------------

    def _start(self, kind: str, **kwargs: object) -> None:
        self._current = Block(kind, **kwargs)  # type: ignore[arg-type]

    def _close(self) -> None:
        block, self._current = self._current, None
        if block is not None and not block.is_empty:
            self.blocks.append(block)

    def _continuous(self) -> bool:
        """Текущий блок продолжается: `<p>` внутри пункта списка его не «рвёт»."""
        return self._current is not None and self._current.kind in {
            KIND_ITEM,
            KIND_QUOTE,
            KIND_CODE,
        }

    def _append(self, text: str) -> None:
        if not text:
            return
        if self._current is None:
            self._start(KIND_PARAGRAPH)
        assert self._current is not None
        self._current.runs.append(
            TextRun(
                text=text,
                bold="bold" in self._marks,
                italic="italic" in self._marks,
                underline="underline" in self._marks,
                strike="strike" in self._marks,
                code="code" in self._marks,
                color=self._color,
                background=self._background,
            )
        )

    def _next_marker(self) -> str:
        """Маркер пункта: «•» или «1.» — нумерация считается по стеку списков."""
        if not self._lists:
            return "•"
        current = self._lists[-1]
        if not current["ordered"]:
            return "•"
        current["index"] = int(current["index"]) + 1
        return f'{current["index"]}.'

    def _apply_style(self, style: str) -> None:
        """Цвет и подсветка из `style`: выравнивание окно кассы игнорирует (§4.6)."""
        for chunk in str(style).split(";"):
            name, _, raw = chunk.partition(":")
            key = name.strip().lower()
            value = raw.strip()
            if not _COLOR.match(value):
                continue
            if key == "color":
                self._color = value
            elif key == "background-color":
                self._background = value


    # --- Разбор -------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {str(name).lower(): str(value or "") for name, value in attrs}
        if tag in _HEADINGS:
            self._close()
            self._start(KIND_HEADING, level=_HEADINGS[tag])
            return
        if tag == "blockquote":
            self._close()
            self._start(KIND_QUOTE)
            return
        if tag == "pre":
            self._close()
            self._start(KIND_CODE)
            return
        if tag in {"ul", "ol"}:
            self._lists.append({"ordered": tag == "ol", "index": 0})
            return
        if tag == "li":
            self._close()
            self._start(KIND_ITEM, marker=self._next_marker())
            return
        if tag in {"p", "div", "tr", "section"}:
            if not self._continuous():
                self._close()
                self._start(KIND_PARAGRAPH)
            return
        if tag == "br":
            self._append("\n")
            return
        if tag == "hr":
            self._close()
            self.blocks.append(Block(KIND_RULE))
            return
        if tag == "img":
            self._close()
            name = _image_name(attributes.get("src"))
            if name:
                self.blocks.append(Block(KIND_IMAGE, name=name, alt=attributes.get("alt", "")))
            return
        if tag in _MARKS:
            self._marks.append(_MARKS[tag])
            return
        if tag == "span" or "style" in attributes:
            self._apply_style(attributes.get("style", ""))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"ul", "ol"}:
            if self._lists:
                self._lists.pop()
            self._close()
            return
        if tag in _HEADINGS or tag in _BLOCK_STARTS or tag == "blockquote":
            self._close()
            return
        if tag in _MARKS:
            mark = _MARKS[tag]
            if mark in self._marks:
                self._marks.remove(mark)
            return
        if tag == "span":
            self._color = None
            self._background = None

    def handle_data(self, data: str) -> None:
        self._append(data)

    def finish(self) -> list[Block]:
        """Закрыть последний блок и вернуть то, что увидит окно кассы."""
        self._close()
        return [block for block in self.blocks if not block.is_empty]


def outline(blocks: Sequence[Block], *, limit: int = 140) -> str:
    """Одна строка для списка уведомлений: текст без разметки, у картинки — подпись."""
    parts: list[str] = []
    for block in blocks:
        if block.kind == KIND_IMAGE:
            parts.append(f'[{block.alt or "картинка"}]')
        elif block.kind == KIND_RULE:
            continue
        else:
            parts.append(" ".join(block.text.split()))
    line = " · ".join(part for part in parts if part)
    if len(line) <= limit:
        return line
    return line[: limit - 1].rstrip() + "…"


def _image_name(src: object) -> str:
    """Имя картинки из метки `magaz-image:<имя>`; пусто — это не наша картинка."""
    text = str(src or "").strip()
    if not text.lower().startswith(IMAGE_SRC_PREFIX):
        return ""
    return text[len(IMAGE_SRC_PREFIX) :].strip()


def body_blocks(body: str, body_format: str = "plain") -> list[Block]:
    """Тело в блоки: HTML — по разметке, простой текст — по строкам (§4.6, Э9)."""
    text = str(body or "")
    if str(body_format or "plain") != "html":
        return [
            Block(KIND_PARAGRAPH, [TextRun(line)]) for line in text.splitlines() if line.strip()
        ]
    parser = _BodyParser()
    parser.feed(text)
    parser.close()
    return parser.finish()
