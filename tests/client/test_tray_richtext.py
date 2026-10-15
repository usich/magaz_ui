"""Тесты разбора разметки для окна кассы (§4.6, §7.4, Э9).

Разбор живёт без `tkinter` и `PIL`, поэтому проверяется здесь: окно кассы получает
готовые блоки и фрагменты, а не «как-нибудь отрисованный HTML».
"""

from __future__ import annotations

from client.tray.richtext import (
    KIND_CODE,
    KIND_HEADING,
    KIND_IMAGE,
    KIND_ITEM,
    KIND_PARAGRAPH,
    KIND_QUOTE,
    KIND_RULE,
    body_blocks,
    outline,
)


class TestBodyBlocks:
    """Тело → блоки: заголовки, списки, цитаты, картинки, оформление (§4.6, Э9)."""

    def test_plain_text_becomes_paragraphs(self) -> None:
        blocks = body_blocks("Смена закрывается\nСохраните смену", "plain")

        assert [block.kind for block in blocks] == [KIND_PARAGRAPH, KIND_PARAGRAPH]
        assert blocks[0].text == "Смена закрывается"
        assert blocks[1].text == "Сохраните смену"

    def test_empty_lines_are_skipped(self) -> None:
        assert [block.text for block in body_blocks("раз\n\nдва", "plain")] == ["раз", "два"]

    def test_headings_and_paragraphs(self) -> None:
        blocks = body_blocks("<h2>Порядок</h2><p>Текст</p>", "html")

        assert [block.kind for block in blocks] == [KIND_HEADING, KIND_PARAGRAPH]
        assert blocks[0].level == 2
        assert blocks[0].text == "Порядок"

    def test_marks_become_run_flags(self) -> None:
        blocks = body_blocks("<p>Обычный <b>жирный</b> и <i>курсив</i></p>", "html")
        runs = blocks[0].runs

        assert runs[0].text == "Обычный "
        assert runs[0].bold is False
        assert runs[1].text == "жирный"
        assert runs[1].bold is True
        assert runs[2].text == " и "
        assert runs[3].text == "курсив"
        assert runs[3].italic is True

    def test_underline_and_strike(self) -> None:
        blocks = body_blocks("<p><u>подчёркнуто</u> и <s>зачёркнуто</s></p>", "html")

        assert blocks[0].runs[0].underline is True
        assert blocks[0].runs[2].strike is True

    def test_lists_get_markers(self) -> None:
        blocks = body_blocks("<ul><li><p>раз</p></li><li><p>два</p></li></ul>", "html")

        assert [block.kind for block in blocks] == [KIND_ITEM, KIND_ITEM]
        assert [block.marker for block in blocks] == ["•", "•"]
        assert [block.text for block in blocks] == ["раз", "два"]

    def test_ordered_list_numbers_items(self) -> None:
        blocks = body_blocks("<ol><li>раз</li><li>два</li></ol>", "html")

        assert [block.marker for block in blocks] == ["1.", "2."]

    def test_quote_and_code(self) -> None:
        blocks = body_blocks("<blockquote>важно</blockquote><pre>код</pre>", "html")

        assert [block.kind for block in blocks] == [KIND_QUOTE, KIND_CODE]

    def test_image_becomes_block_with_name(self) -> None:
        blocks = body_blocks('<p>Смотри</p><img src="magaz-image:shema.png" alt="Схема">', "html")

        assert blocks[-1].kind == KIND_IMAGE
        assert blocks[-1].name == "shema.png"
        assert blocks[-1].alt == "Схема"

    def test_image_width_comes_from_style(self) -> None:
        """Размер картинки задаёт админ в редакторе (§11.5, Э9)."""
        body = (
            '<img src="magaz-image:shema.png" style="width: 50%">'
            '<img src="magaz-image:plan.png" style="width: 320px">'
            '<img src="magaz-image:tak.png" style="width: 100vw">'
            '<img src="magaz-image:net.png">'
        )
        blocks = body_blocks(body, "html")

        assert [block.width for block in blocks] == ["50%", "320px", "", ""]

    def test_foreign_image_is_ignored(self) -> None:
        """Внешний адрес рисовать нечем: касса в интернет не ходит (§7.4)."""
        assert body_blocks('<img src="https://example.com/a.png">', "html") == []

    def test_rule_and_breaks(self) -> None:
        blocks = body_blocks("<p>раз<br>два</p><hr>", "html")

        assert "\n" in blocks[0].text
        assert blocks[-1].kind == KIND_RULE

    def test_colors_are_kept(self) -> None:
        blocks = body_blocks(
            '<p><span style="color:#b91c1c; background-color: #fef08a">Срочно</span></p>',
            "html",
        )
        run = blocks[0].runs[0]

        assert run.color == "#b91c1c"
        assert run.background == "#fef08a"

    def test_unknown_tag_keeps_text(self) -> None:
        blocks = body_blocks("<article>Текст остаётся</article>", "html")

        assert "Текст остаётся" in " ".join(block.text for block in blocks)

    def test_link_text_is_shown(self) -> None:
        blocks = body_blocks('<p><a href="https://example.com">Инструкция</a></p>', "html")

        assert blocks[0].text == "Инструкция"

    def test_entities_are_decoded(self) -> None:
        blocks = body_blocks("<p>Смена &lt; 22:00</p>", "html")

        assert blocks[0].text == "Смена < 22:00"


class TestOutline:
    """Короткая строка для списка уведомлений в окне (§11.5, Э9)."""

    def test_single_line_with_image_label(self) -> None:
        blocks = body_blocks(
            '<p>Схема зала</p><ul><li>Проверьте витрину</li></ul>'
            '<img src="magaz-image:a.png">',
            "html",
        )

        line = outline(blocks)

        assert "Схема зала" in line
        assert "Проверьте витрину" in line
        assert "[картинка]" in line
        assert "\n" not in line

    def test_is_truncated(self) -> None:
        blocks = body_blocks("слово " * 100, "plain")

        assert outline(blocks, limit=30).endswith("…")
        assert len(outline(blocks, limit=30)) <= 30
