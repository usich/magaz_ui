"""Тесты `shared.richtext` — белый список HTML и текстовая версия (Э9, §4.6).

Проверяется не «сколько тегов разрешено», а поведение: опасное содержимое не
должно доехать ни до SPA, ни до окна кассы, а понятное форматирование и картинки —
доехать. Отдельно закреплено, что картинка адресуется только своей меткой
`magaz-image:<имя>`: чужой адрес в теле уведомления — это либо утечка, либо
картинка, которая не откроется на кассе без интернета (§7.4).
"""

from __future__ import annotations

import pytest

from shared.richtext import (
    BODY_FORMAT_HTML,
    BODY_FORMAT_PLAIN,
    html_to_text,
    image_name_from_src,
    image_src,
    is_safe_image_name,
    normalize_body_format,
    plain_text,
    sanitize_html,
)


class TestSanitizeHtml:
    """Разметка из редактора: разрешённое остаётся, опасное исчезает."""

    def test_keeps_allowed_markup(self) -> None:
        body = '<p>Важно: <b>обновление</b> <a href="https://1c.local/faq">инструкция</a></p>'

        assert sanitize_html(body) == body

    def test_void_tags_are_closed(self) -> None:
        assert sanitize_html("строка<br>вторая<hr>") == "строка<br />вторая<hr />"

    def test_script_is_dropped_with_content(self) -> None:
        assert sanitize_html("<p>до</p><script>alert('x')</script><p>после</p>") == (
            "<p>до</p><p>после</p>"
        )

    def test_unknown_tag_is_unwrapped_but_text_stays(self) -> None:
        assert sanitize_html("<custom>текст</custom>") == "текст"

    def test_unknown_attributes_are_dropped(self) -> None:
        assert sanitize_html('<p onclick="evil()">текст</p>') == "<p>текст</p>"

    def test_text_is_escaped(self) -> None:
        assert sanitize_html("<p>5 < 6 & 7 > 6</p>") == "<p>5 &lt; 6 &amp; 7 &gt; 6</p>"

    def test_javascript_link_loses_href(self) -> None:
        assert sanitize_html('<a href="javascript:alert(1)">клик</a>') == "<a>клик</a>"

    def test_mailto_and_http_links_survive(self) -> None:
        assert sanitize_html('<a href="mailto:it@magaz.local">почта</a>') == (
            '<a href="mailto:it@magaz.local">почта</a>'
        )

    def test_style_keeps_only_safe_properties(self) -> None:
        body = '<p style="color: #c00; position: fixed; text-align: center">текст</p>'

        assert sanitize_html(body) == '<p style="color: #c00; text-align: center">текст</p>'

    def test_own_image_survives_and_foreign_is_dropped(self) -> None:
        source = f'<img src="{image_src("kassa.png")}" alt="касса" width="320">'
        expected = f'<img src="{image_src("kassa.png")}" alt="касса" width="320" />'
        foreign = '<img src="https://example.com/logo.png">'

        assert sanitize_html(source) == expected
        assert sanitize_html(f"<p>см.</p>{foreign}") == "<p>см.</p>"

    def test_image_without_src_is_dropped(self) -> None:
        assert sanitize_html('<img alt="пусто">') == ""

    def test_image_size_must_be_numeric(self) -> None:
        body = f'<img src="{image_src("a.png")}" width="100%" height="12">'

        assert sanitize_html(body) == f'<img src="{image_src("a.png")}" height="12" />'


class TestAllowedImages:
    """Метка картинки, для которой файла нет, до потребителя не доезжает (§4.6, Э9)."""

    def test_known_image_survives(self) -> None:
        body = f'<p>Смотри</p><img src="{image_src("shema.png")}">'

        assert image_src("shema.png") in sanitize_html(body, allowed_images={"shema.png"})

    def test_unknown_image_is_dropped(self) -> None:
        body = (
            "<p>Смотри</p>"
            f'<img src="{image_src("est.png")}">'
            f'<img src="{image_src("net.png")}">'
        )

        cleaned = sanitize_html(body, allowed_images={"est.png"})

        assert "est.png" in cleaned
        assert "net.png" not in cleaned
        assert "Смотри" in cleaned

    def test_empty_set_drops_every_image(self) -> None:
        assert sanitize_html(f'<img src="{image_src("a.png")}">', allowed_images=set()) == ""

    def test_without_the_check_images_stay(self) -> None:
        """Первый проход санитайза идёт до сохранения картинок — проверять нечего."""
        assert image_src("a.png") in sanitize_html(f'<img src="{image_src("a.png")}">')



class TestHtmlToText:
    """Текстовая версия: то, что уходит в `msg.exe`, логи и аудит (§4.4)."""

    def test_paragraphs_and_breaks_become_lines(self) -> None:
        assert html_to_text("<p>Первая</p><p>Вторая<br>и продолжение</p>") == (
            "Первая\nВторая\nи продолжение"
        )

    def test_list_gets_bullets(self) -> None:
        assert html_to_text("<ul><li>раз</li><li>два</li></ul>") == "• раз\n• два"

    def test_image_shows_alt_or_placeholder(self) -> None:
        assert "[схема зала]" in html_to_text(
            f'<p>Смотри</p><img src="{image_src("a.png")}" alt="схема зала">'
        )
        assert "[картинка]" in html_to_text(f'<img src="{image_src("a.png")}">')

    def test_plain_text_keeps_plain_body(self) -> None:
        assert plain_text("просто текст", BODY_FORMAT_PLAIN) == "просто текст"
        assert plain_text("<p>разметка</p>", BODY_FORMAT_HTML) == "разметка"


class TestBodyFormat:
    """Формат тела: старые уведомления остаются `plain`, новые могут быть `html`."""

    def test_default_is_plain(self) -> None:
        assert normalize_body_format(None) == BODY_FORMAT_PLAIN
        assert normalize_body_format("") == BODY_FORMAT_PLAIN

    def test_html_is_accepted_case_insensitively(self) -> None:
        assert normalize_body_format(" HTML ") == BODY_FORMAT_HTML

    def test_unknown_format_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="формат тела"):
            normalize_body_format("markdown")


class TestImageNames:
    """Имя картинки — это имя файла: без путей и спецсимволов."""

    def test_roundtrip(self) -> None:
        assert image_name_from_src(image_src("kassa-1.png")) == "kassa-1.png"

    def test_foreign_src_is_not_ours(self) -> None:
        assert image_name_from_src("https://example.com/a.png") is None
        assert image_name_from_src(None) is None

    def test_traversal_is_rejected(self) -> None:
        assert not is_safe_image_name("../secret.png")
        assert not is_safe_image_name("a/b.png")
        assert image_name_from_src(f"{image_src('../secret.png')}") is None
