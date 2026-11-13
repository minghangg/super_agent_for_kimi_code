"""The questionnaire page: semantic structure, tokens, and the rules it must not break.

Every pin here exists because the page is read by a person, and the failures it
guards against are invisible in a screenshot:

* a missing ``<head>`` puts ``<meta charset>`` in the body quirk tree, so the
  encoding is guessed rather than declared;
* a ``<title>`` outside ``<head>`` is not the document title at all - browsers
  synthesise one, and the tab shows the URL;
* ``<h2>`` with no ``<h1>`` makes the outline start at level two, so a screen
  reader's heading list begins mid-document;
* ``<blockquote>`` for a question announces a quotation, and ``<b>`` announces
  "visually offset" rather than "important";
* a ``<textarea>`` with no ``<label>`` announces an unnamed edit field;
* an inline ``style`` cannot be themed and cannot be overridden;
* ``color-scheme`` on ``body`` leaves the viewport surfaces - scrollbars, the
  canvas, form controls - in light mode even when the page is dark;
* a literal colour outside the token block is a colour the dark theme will miss;
* ``box-shadow`` is what makes a document look like an admin console;
* ``all: unset`` swallows the focus ring, and ``touch-action: none`` kills
  scrolling gestures for a page that has none to replace them with.

The assertions read the **rendered** page (``_page`` / ``_form``), not the
template strings, so a change in how the pieces are assembled is covered too.

Run: ``python -m pytest tests/test_at_serve_page.py -q``
"""
from __future__ import annotations

import re
import unittest

from super_agent import at_serve

RECORD = {"id": "abc123def456", "thread": "owner/demo#64", "issue": 64,
          "comment_id": 9100, "question": "先做 A 还是 B？", "sid": "session_at"}


def render(body: str = "", notice: str = "") -> str:
    return at_serve._page(RECORD, body or at_serve._form(RECORD), notice)


def css(page: str) -> str:
    match = re.search(r"<style>(.*?)</style>", page, re.S)
    return match.group(1) if match else ""


class DocumentStructure(unittest.TestCase):
    """P1-P3: a real document, with one top-level heading."""

    def test_p1_head_exists_and_charset_is_inside_it(self):
        page = render()
        head = re.search(r"<head>(.*?)</head>", page, re.S)
        self.assertIsNotNone(head, "the page needs a <head>")
        self.assertIn('<meta charset="utf-8">', head.group(1),
                      "the charset must be declared inside <head>")

    def test_p1b_the_head_comes_before_the_body(self):
        page = render()
        self.assertLess(page.index("<head>"), page.index("<body>"))

    def test_p2_the_title_is_inside_the_head(self):
        page = render()
        head = re.search(r"<head>(.*?)</head>", page, re.S).group(1)
        self.assertIn("<title>", head)
        self.assertEqual(page.count("<title>"), 1)
        body = re.search(r"<body>(.*?)</body>", page, re.S).group(1)
        self.assertNotIn("<title>", body)

    def test_p3_exactly_one_h1_and_no_heading_jump(self):
        page = render()
        self.assertEqual(page.count("<h1>"), 1)
        self.assertNotIn("<h2>", page,
                         "a page with one heading level needs no <h2>")
        body = re.search(r"<body>(.*?)</body>", page, re.S).group(1)
        self.assertLess(body.index("<h1>"), body.index("<form"),
                        "the heading is the page's subject, above the form")

    def test_the_page_declares_the_language(self):
        self.assertIn('<html lang="zh-CN">', render())

    def test_the_page_declares_a_viewport(self):
        self.assertIn('name="viewport"', render())


class Semantics(unittest.TestCase):
    """P4/P5: no quotation markup for a question, and a labelled field."""

    def test_p4_no_blockquote_and_no_b(self):
        for page in (render(), render(notice=at_serve._notice("x", warn=True))):
            self.assertNotIn("<blockquote", page)
            self.assertNotIn("<b>", page, "<strong> carries emphasis")
        # The heading block uses <strong> where the old page used <b>.
        self.assertIn("<strong>", render())

    def test_p5_the_textarea_is_labelled_by_id(self):
        page = render()
        field = re.search(r"<textarea\b([^>]*)>", page)
        self.assertIsNotNone(field, "the form needs a textarea")
        attrs = field.group(1)
        ident = re.search(r'id="([^"]+)"', attrs)
        self.assertIsNotNone(ident, "a label can only point at an id")
        self.assertIn(f'<label for="{ident.group(1)}">', page,
                      "label and control must be associated by for/id")

    def test_p5b_the_label_comes_before_the_control(self):
        page = render()
        self.assertLess(page.index("<label"), page.index("<textarea"))

    def test_p5c_the_hint_is_described_not_packed_into_the_label(self):
        # A verbose label is read on every focus; instructions belong in
        # ``aria-describedby`` so they can be skipped.
        page = render()
        self.assertIn('aria-describedby="hint"', page)
        self.assertIn('id="hint"', page)
        label = re.search(r"<label[^>]*>(.*?)</label>", page, re.S).group(1)
        self.assertNotIn("回传", label)

    def test_the_field_is_required(self):
        self.assertIn("required", render())


class Styling(unittest.TestCase):
    """P6-P9: no inline style, a token-only palette, and no shadow."""

    def test_p6_no_inline_style_attribute(self):
        for page in (render(), render(notice=at_serve._notice("x", warn=True))):
            self.assertNotIn(" style=", page,
                             "an inline style cannot be themed or overridden")

    def test_p7_color_scheme_in_meta_and_on_root(self):
        page = render()
        self.assertIn('<meta name="color-scheme" content="light dark">', page)
        sheet = css(page)
        root_block = re.search(r":root\s*\{(.*?)\}", sheet, re.S)
        self.assertIsNotNone(root_block)
        self.assertIn("color-scheme: light dark", root_block.group(1),
                      "on <body> it would leave viewport surfaces in light mode")

    def test_p8_the_palette_is_tokenised(self):
        sheet = css(page_css := render())
        for token in ("--ui-paper", "--ui-paper-2", "--ui-surface", "--ui-ink",
                      "--ui-ink-strong", "--ui-muted", "--ui-rule",
                      "--ui-accent", "--ui-radius-card", "--ui-radius-ctl",
                      "--ui-space-1", "--ui-space-6"):
            self.assertIn(token + ":", sheet, token)
        # Every literal colour lives in a token block. Anything outside one is
        # a colour the dark theme cannot reach.
        without_tokens = re.sub(r":root\s*\{.*?\}", "", sheet, flags=re.S)
        without_tokens = re.sub(r"@media[^{]*\{\s*:root\s*\{.*?\}\s*\}",
                                "", without_tokens, flags=re.S)
        literals = re.findall(r"#[0-9a-fA-F]{3,8}\b", without_tokens)
        self.assertEqual(literals, [], "hardcoded colours outside the tokens")
        del page_css

    def test_p8b_the_one_banned_token_is_absent(self):
        # ``--ui-soft`` measures 2.83:1: fine for decoration, unreadable as text.
        # The page does not need a decorative-only colour, so it is not defined.
        self.assertNotIn("--ui-soft", css(render()))

    def test_p9_no_box_shadow(self):
        page = render()
        self.assertNotIn("box-shadow", page,
                         "a shadow is what makes a page look like a console")


class Interaction(unittest.TestCase):
    """P10/P11: a visible focus ring, and the properties that would eat it."""

    def test_p10_focus_visible_uses_an_outline(self):
        sheet = css(render())
        rule = re.search(r"button:focus-visible[^{]*\{(.*?)\}", sheet, re.S)
        self.assertIsNotNone(rule, "the button needs a :focus-visible style")
        self.assertIn("outline:", rule.group(1))
        self.assertIn("outline-offset:", rule.group(1))
        self.assertNotIn("outline: none", sheet)
        self.assertNotIn("outline:none", sheet)

    def test_p10b_the_textarea_gets_the_same_ring(self):
        sheet = css(render())
        rule = re.search(r"textarea:focus-visible[^{]*\{(.*?)\}", sheet, re.S)
        self.assertIsNotNone(rule, "the field needs a :focus-visible style")
        self.assertIn("outline:", rule.group(1))

    def test_p11_no_all_unset_and_no_touch_action_none(self):
        page = render()
        self.assertNotIn("all: unset", page)
        self.assertNotIn("all:unset", page)
        self.assertNotIn("touch-action: none", page)
        self.assertNotIn("touch-action:none", page)

    def test_p11b_no_touch_action_at_all(self):
        # The page has no gestures and one scrolling field; the property would
        # be cargo cult. If one is ever needed it must be per-axis.
        self.assertNotIn("touch-action", css(render()))


class LayoutDiscipline(unittest.TestCase):
    """The typographic rules that make it read as a document."""

    def test_the_serif_heading_and_mono_eyebrow(self):
        sheet = css(render())
        h1 = re.search(r"\bh1\s*\{(.*?)\}", sheet, re.S).group(1)
        self.assertIn("Georgia", h1)
        self.assertIn("2.25rem", h1, "36px written as rem")
        self.assertNotIn("36px", sheet)
        eyebrow = re.search(r"\.eyebrow\s*\{(.*?)\}", sheet, re.S).group(1)
        self.assertIn("ui-monospace", eyebrow)
        self.assertIn("letter-spacing: .12em", eyebrow)
        self.assertIn("text-transform: uppercase", eyebrow)
        self.assertIn("font-size: .6875rem", eyebrow)

    def test_the_paper_background_and_hairline(self):
        sheet = css(render())
        body = re.search(r"\bbody\s*\{(.*?)\}", sheet, re.S).group(1)
        self.assertIn("background: var(--ui-paper)", body)
        self.assertIn("1px solid var(--ui-rule)", sheet)

    def test_the_radii_come_from_tokens(self):
        sheet = css(render())
        self.assertIn("--ui-radius-card:  10px", sheet)
        self.assertIn("--ui-radius-ctl:   6px", sheet)
        self.assertIn("border-radius: var(--ui-radius-card)", sheet)
        self.assertIn("border-radius: var(--ui-radius-ctl)", sheet)

    def test_spacing_uses_the_scale(self):
        sheet = css(render())
        self.assertIn("padding: var(--ui-space-", sheet)
        self.assertIn("margin:", sheet)
        # No bare pixel **spacing**. Two exceptions are allowed and both are
        # stated here: the 1px hairline, and the 2px focus offset, which is a
        # ring geometry value rather than a step on the 4px scale.
        outside_tokens = re.sub(r":root\s*\{.*?\}", "", sheet, flags=re.S)
        outside_tokens = re.sub(r"@media[^{]*\{\s*:root\s*\{.*?\}\s*\}",
                                "", outside_tokens, flags=re.S)
        spacing_props = re.findall(
            r"(?:margin|padding|gap)[^;{}]*:\s*([^;}]+)", outside_tokens)
        stray = [value for value in spacing_props if re.search(r"\d+px", value)]
        self.assertEqual(stray, [], f"spacing must use --ui-space-*: {stray}")

    def test_the_accent_is_the_only_colour_carrier(self):
        sheet = css(render())
        outside_tokens = re.sub(r":root\s*\{.*?\}", "", sheet, flags=re.S)
        for prop, token in (("background: var(--ui-paper)", "--ui-paper"),
                            ("color: var(--ui-ink)", "--ui-ink")):
            self.assertIn(prop, outside_tokens, token)
        self.assertIn("color: var(--ui-accent)", sheet)
        self.assertIn("border: 1px solid var(--ui-accent)", sheet)

    def test_status_is_not_conveyed_by_fill(self):
        # A warn notice changes its border and weight, never its background.
        warn = at_serve._notice("答案已保存", warn=True)
        self.assertIn('data-state="warn"', warn)
        sheet = css(render())
        rule = re.search(r'\.notice\[data-state="warn"\]\s*\{(.*?)\}',
                         sheet, re.S).group(1)
        self.assertIn("border-color:", rule)
        self.assertNotIn("background", rule)

    def test_no_external_font_or_cdn(self):
        page = render()
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("<link", page)
        self.assertNotIn("@import", page)


class NoticeRendering(unittest.TestCase):
    """The status line keeps its wording, which the E2E tests assert on."""

    def test_a_warning_says_it_did_not_send(self):
        text = "答案已保存，但没有回传：本题没有登记 AT 会话。"
        page = render(notice=at_serve._notice(text, warn=True))
        self.assertIn("没有回传", page)
        self.assertNotIn("已提交", page)

    def test_a_success_says_it_was_submitted(self):
        page = render(notice=at_serve._notice("已提交，答案已回传 AT。"))
        self.assertIn("已提交", page)

    def test_the_question_is_escaped(self):
        record = dict(RECORD, question="<script>alert(1)</script>")
        page = at_serve._page(record, "")
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)


if __name__ == "__main__":
    unittest.main()
