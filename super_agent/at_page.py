"""The AT questionnaire page: skeleton, form and answer summary.

Split out of ``at_serve`` (ISS-132 D): everything here is a pure function of a
question record - no file, no clock, no network. The service half
(load / validate / write / notify) is where the failure reasoning lives, and 290
lines of markup in front of it hid that. The page itself is unchanged: same
strings, same escaping, same submit wording.

The stylesheet lives in :mod:`at_style` and is inserted here as a value, so this
module never doubles a brace. ``_STYLE`` is re-exported because ``at_serve``
exposes it under that name.
"""
from __future__ import annotations

import html

from .at_style import _STYLE  # noqa: E402,F401  re-exported for at_serve

_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>#{issue} AT 需求确认 · r{revision}</title>
<style>{style}</style>
</head>
<body>
<main>
<p class="eyebrow">{project} · AT 需求确认</p>
<h1>确认你的选择</h1>
<p class="meta"><strong>#{issue} · revision {revision}</strong><br>{thread}（问题 cid {cid}）</p>
<section class="q" aria-labelledby="question-heading">
<p class="eyebrow">本轮一个问题</p>
<h2 id="question-heading">{question}</h2>
{objective}
</section>
{notice}
{body}
</main>
</body>
</html>
"""

_FORM = """<form method="post" action="/q/{qid}" aria-labelledby="question-heading">
{options}
{custom}
<div class="submit-row">
<p class="hint">确认后保存全文，并通知当前 AT。重复提交不会重复发送。</p>
<button type="submit">确认并回答 AT</button>
</div>
</form>"""


def _notice(text: str, *, warn: bool = False) -> str:
    state = ' data-state="warn" role="alert"' if warn else ' role="status"'
    return f'<p class="notice"{state}>{text}</p>'


def _page(record: dict, body: str, notice: str = "") -> str:
    thread = str(record.get("thread") or "")
    objective = str(record.get("objective") or "")
    objective_html = (f'<p class="hint">目标：{html.escape(objective)}</p>'
                      if objective else "")
    return _PAGE.format(
        style=_STYLE, project=html.escape(thread.split("#", 1)[0] or "AT"),
        issue=html.escape(str(record.get("issue") or "")),
        revision=html.escape(str(record.get("revision") or 0)),
        thread=html.escape(thread),
        cid=html.escape(str(record.get("comment_id") or "")),
        question=html.escape(str(record.get("question") or "")),
        objective=objective_html, notice=notice, body=body)


def _form(record: dict) -> str:
    interaction = record.get("interaction") or {}
    options = interaction.get("options") or []
    selected = set(record.get("choices") or [])
    rows = []
    input_type = "checkbox" if interaction.get("choice_mode") == "multiple" else "radio"
    for index, option in enumerate(options):
        option_id = str(option.get("id") or "")
        control_id = f"choice-{index}"
        checked = " checked" if option_id in selected else ""
        description = str(option.get("description") or "")
        describedby = f' aria-describedby="{control_id}-description"' if description else ""
        description_html = (f'<span id="{control_id}-description" class="option-description">'
                            f'{html.escape(description)}</span>' if description else "")
        rows.append(
            f'<div class="option"><input type="{input_type}" id="{control_id}" '
            f'name="choice" value="{html.escape(option_id)}"{checked}{describedby}>'
            f'<label for="{control_id}"><span class="option-title">'
            f'{html.escape(str(option.get("label") or option_id))}</span>'
            f'{description_html}</label></div>')
    options_html = ""
    if rows:
        legend = "选择一个或多个选项" if input_type == "checkbox" else "选择一个选项"
        options_html = (f'<fieldset><legend>{legend}</legend>'
                        f'<p id="choice-hint" class="hint">可只补充说明；选项与补充会一起保存。</p>'
                        f'<div class="options">{"".join(rows)}</div></fieldset>')
    custom_html = ""
    if interaction.get("allow_custom", True):
        label = "补充说明" if rows else "你的回答"
        required = "" if rows else " required"
        custom_html = (f'<div class="supplement"><label for="answer">{label}</label>'
                       f'<p id="answer-hint" class="hint">填写约束、偏好或其他选项没有覆盖的内容。</p>'
                       f'<textarea id="answer" name="answer" rows="5"{required} '
                       f'aria-describedby="answer-hint">{html.escape(str(record.get("answer") or ""))}</textarea></div>')
    return _FORM.format(qid=html.escape(str(record.get("id") or "")),
                        options=options_html, custom=custom_html)


def _answer_summary(record: dict) -> str:
    selected = record.get("choice_labels") or []
    parts = []
    if selected:
        parts.append("<p><strong>已选：</strong>" + html.escape("；".join(selected)) + "</p>")
    if record.get("answer"):
        parts.append("<p><strong>补充：</strong></p><pre class=\"answer\">"
                     + html.escape(str(record.get("answer"))) + "</pre>")
    return "".join(parts)
