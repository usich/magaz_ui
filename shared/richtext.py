"""Форматированный текст уведомлений (§4.6, Э9): HTML по белому списку и текстовая версия.

Тело уведомления пишется в редакторе админки (TipTap → HTML) и рисуется в двух
местах: в SPA (предпросмотр и отчёт §11.5) и в окне кассы (§7.4). Оба потребителя
показывают разметку, поэтому единственная точка доверия — сервер: HTML проходит
белый список тегов и атрибутов, а не «как нарисовал редактор».

Правила:

* разрешённые теги — из `ALLOWED_TAGS`; неизвестный тег не съедает текст:
  разметка снимается, содержимое остаётся видимым;
* опасные теги (`script`, `style`, `iframe`, …) выбрасываются вместе с
  содержимым — показывать их текст пользователю нельзя;
* ссылки — только `http(s)://` и `mailto:`: `javascript:` и `data:` отбрасываются;
* картинки адресуются меткой `magaz-image:<имя файла>`. Конкретный адрес
  подставляет потребитель: SPA — `/api/v1/admin/notifications/...`, касса — свой
  локальный кэш. Внешних адресов в теле быть не может: касса не ходит в интернет,
  а картинка должна быть видна и без сети (§7.4);
* метка без файла до потребителя не доезжает: при сохранении уведомления
  `sanitize_html(..., allowed_images=…)` выбрасывает `img`, для которых картинку
  не приложили, — лучше без картинки, чем с битой (§4.6, §7.4);
* `style` сохраняется частично: только безопасные свойства (`color`,
  `background-color`, `text-align`, `font-weight`, `text-decoration`, а для
  картинок ещё `width`/`max-width` — это размер из редактора, §11.5), поэтому
  «важный текст красным» из редактора доезжает, а `position:fixed` — нет.

`html_to_text()` нужен там, где форматирование не поддерживается: дублирующий
канал `msg.exe` (§4.4), логи и журнал аудита.
"""

from __future__ import annotations

import html
import re
from collections.abc import Collection
from html.parser import HTMLParser

__all__ = [
    "ALLOWED_ATTRIBUTES",
    "ALLOWED_TAGS",
    "BODY_FORMATS",
    "BODY_FORMAT_HTML",
    "BODY_FORMAT_PLAIN",
    "DROPPED_TAGS",
    "IMAGE_NAME_MAX_LENGTH",
    "IMAGE_SRC_PREFIX",
    "VOID_TAGS",
    "html_to_text",
    "image_name_from_src",
    "image_src",
    "is_safe_image_name",
    "normalize_body_format",
    "plain_text",
    "sanitize_html",
]

#: Тело без форматирования (как было до Э9) и тело с HTML из редактора
BODY_FORMAT_PLAIN = "plain"
BODY_FORMAT_HTML = "html"
BODY_FORMATS: tuple[str, ...] = (BODY_FORMAT_PLAIN, BODY_FORMAT_HTML)

#: Имя файла картинки: то, что попадает в `magaz-image:<имя>` и в URL
IMAGE_NAME_MAX_LENGTH = 64
IMAGE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: Метка картинки в теле: адрес подставляет потребитель (SPA, касса)
IMAGE_SRC_PREFIX = "magaz-image:"

#: Разрешённая разметка: абзацы, списки, выделения, ссылки, таблицы, картинки
ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "a", "b", "blockquote", "br", "code", "del", "div", "em", "h1", "h2", "h3", "h4", "hr",
        "i", "img", "ins", "li", "ol", "p", "pre", "s", "span", "strike", "strong", "sub", "sup",
        "table", "tbody", "td", "th", "thead", "tr", "u", "ul",
    }
)
VOID_TAGS: frozenset[str] = frozenset({"br", "hr", "img"})

#: Теги, которые выбрасываются вместе с содержимым: их текст показывать нельзя
DROPPED_TAGS: frozenset[str] = frozenset(
    {
        "applet", "audio", "base", "button", "canvas", "embed", "form", "frame", "frameset",
        "iframe", "input", "link", "math", "meta", "noscript", "object", "option", "script",
        "select", "source", "style", "svg", "template", "textarea", "video",
    }
)

#: Разрешённые атрибуты по тегу; `*` — общие для всех разрешённых тегов
ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "title", "width", "height"}),
    "*": frozenset({"style"}),
}

_SAFE_URL_PREFIXES = ("http://", "https://", "mailto:")
_COLOR = re.compile(
    r"^(#[0-9a-fA-F]{3,8}"
    r"|rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(,\s*[\d.]+\s*)?\)"
    r"|[a-zA-Z]{3,20})$"
)
_SIZE = re.compile(r"^\d{1,4}$")
#: Длина в стиле: только число с `px`/`%` — «100vw» и «calc(...)» не проходят (§4.6, Э9)
_LENGTH = re.compile(r"^\d{1,4}(px|%)$")
_ALLOWED_STYLE: dict[str, re.Pattern[str]] = {
    "color": _COLOR,
    "background-color": _COLOR,
    "text-align": re.compile(r"^(left|right|center|justify)$"),
    "font-weight": re.compile(r"^(normal|bold|[1-9]00)$"),
    "text-decoration": re.compile(r"^(none|underline|line-through)$"),
    # Размер картинки из редактора (§11.5): сервер пропускает его, только если это
    # безопасная длина — «картинка во всю ширину» доезжает до кассы, `100vw` — нет
    "width": _LENGTH,
    "max-width": _LENGTH,
}
_BLOCK_TAGS = frozenset(
    {
        "blockquote", "div", "h1", "h2", "h3", "h4", "hr", "li", "ol", "p", "pre", "table", "tr",
        "ul",
    }
)


def normalize_body_format(value: object) -> str:
    """Формат тела: `plain` (по умолчанию) или `html` (§4.6, Э9)."""
    text = str(value or "").strip().lower()
    if not text:
        return BODY_FORMAT_PLAIN
    if text not in BODY_FORMATS:
        raise ValueError("Недопустимый формат тела: ожидается plain или html")
    return text


def is_safe_image_name(name: str) -> bool:
    """Имя картинки без пути и спецсимволов — так файл нельзя вынести из каталога."""
    return bool(IMAGE_NAME_PATTERN.match(name or ""))


def image_src(name: str) -> str:
    """Метка картинки для тела уведомления: `magaz-image:<имя>`."""
    return f"{IMAGE_SRC_PREFIX}{name}"


def image_name_from_src(src: object) -> str | None:
    """Имя картинки из метки или `None`, если это не наша картинка."""
    text = str(src or "").strip()
    if not text.startswith(IMAGE_SRC_PREFIX):
        return None
    name = text[len(IMAGE_SRC_PREFIX) :].strip()
    return name if is_safe_image_name(name) else None


def plain_text(body: str, body_format: str = BODY_FORMAT_PLAIN) -> str:
    """Текстовая версия тела: HTML разбирается, готовый текст возвращается как есть."""
    if normalize_body_format(body_format) == BODY_FORMAT_HTML:
        return html_to_text(body)
    return body


def sanitize_html(
    value: str,
    *,
    allowed_tags: frozenset[str] | None = None,
    allowed_images: Collection[str] | None = None,
) -> str:
    """Очистить HTML по белому списку тегов и атрибутов (§4.6, Э9).

    `allowed_images` — имена картинок, которые действительно лежат на сервере:
    метка `magaz-image:<имя>` без файла нарисовалась бы на кассе битой картинкой,
    поэтому такие `img` выбрасываются вместе с тегом. `None` — проверять нечего
    (первый проход, до сохранения картинок).
    """
    parser = _Sanitizer(allowed_tags or ALLOWED_TAGS, allowed_images=allowed_images)
    parser.feed(str(value or ""))
    parser.close()
    return "".join(parser.parts).strip()


def html_to_text(value: str) -> str:
    """Прочитать HTML как текст: абзацы и списки — отдельными строками."""
    parser = _TextExtractor()
    parser.feed(str(value or ""))
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()





class _Sanitizer(HTMLParser):
    """Пересобрать разметку только из разрешённых тегов и атрибутов."""

    def __init__(
        self,
        allowed_tags: frozenset[str],
        *,
        allowed_images: Collection[str] | None = None,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.allowed_tags = allowed_tags
        #: `None` — картинки ещё не сохранены, проверять нечего
        self.allowed_images = None if allowed_images is None else set(allowed_images)
        self.parts: list[str] = []
        self._dropped_depth = 0

    # --- Атрибуты -----------------------------------------------------------

    def _clean_attributes(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        """Разрешённые атрибуты тега: ссылки, картинки и безопасные стили."""
        allowed = ALLOWED_ATTRIBUTES.get(tag, frozenset()) | ALLOWED_ATTRIBUTES["*"]
        cleaned: list[str] = []
        for name, value in attrs:
            key = str(name or "").strip().lower()
            raw = str(value or "").strip()
            if key not in allowed or not raw:
                continue
            if key == "style":
                style = _clean_style(raw)
                if style:
                    cleaned.append(f'style="{html.escape(style, quote=True)}"')
                continue
            if key == "href":
                if not raw.lower().startswith(_SAFE_URL_PREFIXES):
                    continue
            elif key in {"width", "height"}:
                if not _SIZE.match(raw):
                    continue
            elif key == "src":
                name = image_name_from_src(raw)
                if name is None:
                    continue
                raw = image_src(name)
            cleaned.append(f'{key}="{html.escape(raw, quote=True)}"')
        return (" " + " ".join(cleaned)) if cleaned else ""

    def _image_name(self, attrs: list[tuple[str, str | None]]) -> str | None:
        """Имя картинки из `src`-метки; без своего `src` рисовать нечего."""
        for name, value in attrs:
            if str(name or "").strip().lower() == "src":
                return image_name_from_src(value)
        return None

    # --- Разбор разметки ----------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Открывающий тег: разрешён — пишем, опасен — начинаем пропуск."""
        if tag in DROPPED_TAGS:
            self._dropped_depth += 1
            return
        if self._dropped_depth or tag not in self.allowed_tags:
            return
        if tag == "img":
            name = self._image_name(attrs)
            if name is None:
                return
            # Метка без файла — битая картинка на кассе (§4.6, Э9)
            if self.allowed_images is not None and name not in self.allowed_images:
                return
        attributes = self._clean_attributes(tag, attrs)
        suffix = " />" if tag in VOID_TAGS else ">"
        self.parts.append(f"<{tag}{attributes}{suffix}")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Самозакрытый тег (`<br/>`, `<img/>`) — то же, что открывающий для `void`."""
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        """Закрывающий тег: опасный завершает пропуск, лишний просто игнорируется."""
        if tag in DROPPED_TAGS:
            self._dropped_depth = max(0, self._dropped_depth - 1)
            return
        if self._dropped_depth or tag not in self.allowed_tags or tag in VOID_TAGS:
            return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        """Текст экранируется, поэтому `<` из редактора не станет разметкой."""
        if self._dropped_depth or not data:
            return
        self.parts.append(html.escape(data, quote=False))


class _TextExtractor(HTMLParser):
    """Собрать текст: блочные теги и `<br>` дают перевод строки, список — маркер."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "li":
            self.parts.append("\n• ")
        elif tag in _BLOCK_TAGS or tag == "br":
            self.parts.append("\n")
        elif tag == "img":
            self.parts.append(f"\n[{_image_label(attrs)}]\n")
        elif tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _image_label(attrs: list[tuple[str, str | None]]) -> str:
    """Подпись картинки в текстовой версии: `alt` редактора или пояснение."""
    for name, value in attrs:
        if str(name or "").strip().lower() == "alt" and str(value or "").strip():
            return str(value).strip()
    return "картинка"


def _clean_style(value: str) -> str:
    """Оставить только безопасные свойства стиля: остальное отбрасывается."""
    kept: list[str] = []
    for chunk in value.split(";"):
        if ":" not in chunk:
            continue
        name, _, raw = chunk.partition(":")
        key = name.strip().lower()
        pattern = _ALLOWED_STYLE.get(key)
        if pattern is None:
            continue
        candidate = " ".join(raw.split())
        if pattern.match(candidate):
            kept.append(f"{key}: {candidate}")
    return "; ".join(kept)
