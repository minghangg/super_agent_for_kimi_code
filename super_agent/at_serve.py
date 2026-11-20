"""Smallest possible questionnaire on loopback, for an AT's ``questions``.

One AT verdict of the ``questions`` kind becomes one link. A person opens the
link, types an answer, submits it, and the answer is sent back to the AT
session. That is the whole feature, and everything below exists to make those
four facts true - not to become a survey product.

Deliberately absent:

* **no authentication**. The service binds ``127.0.0.1`` and refuses every
  other address in :func:`serve`. Adding a login to a loopback form would add
  a credential to store and leak without changing who can reach the port;
* **no background daemon and no multi-question management**. One invocation
  serves the files already on disk under ``<root>/atserve/questions``; a
  questionnaire is a file, so filing one does not need the service;
* **no "submitted" when it was not submitted**. The page says ``已提交`` only
  when the send actually succeeded. A down service, a missing transport, or a
  raising transport each get their own plainly-worded page, because the one
  failure this feature must never produce is a person believing an answer
  reached the AT when it did not.

The send transport is **injected**. Nothing here imports a console client and
nothing falls back to one: the engine is meant to run with ``dispatch`` off,
and a service that quietly talks to production would be the same mistake in a
new place. ``python -m super_agent.at_serve --allow-send`` is the only switch
that wires the real client, and it has to be typed.
"""
from __future__ import annotations

import html
import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 58627/58628/58800/8787/58730 are taken elsewhere in this repository.
DEFAULT_PORT = 58731
HOST = "127.0.0.1"
SUBMIT_TIMEOUT_S = 60.0


def _dir(root) -> Path:
    return Path(root) / "atserve" / "questions"


def _path(root, qid: str) -> Path:
    return _dir(root) / f"{qid}.json"


def create_question(*, root, thread: str, issue: int, comment_id: int,
                    sid: str, question: str, requirement: dict | None = None,
                    binding_keys: list[str] | None = None) -> str:
    """File one question and return its id. Needs no running service.

    AT-mode questions carry the exact requirement revision and every binding
    key that may resolve the current AT. Legacy questions remain readable.
    """
    qid = uuid.uuid4().hex[:12]
    record = {"id": qid, "thread": str(thread), "issue": int(issue),
              "comment_id": int(comment_id), "sid": str(sid or ""),
              "question": str(question or ""), "answer": "",
              "status": "pending", "submitted": False, "created_at": _now(),
              "binding_keys": list(binding_keys or [str(thread)])}
    if requirement:
        record.update(requirement_id=requirement.get("requirement_id"),
                      revision=requirement.get("revision"),
                      requirement_hash=requirement.get("requirement_hash"),
                      objective=str(requirement.get("objective") or ""),
                      interaction=dict(requirement.get("interaction") or {}))
    path = _path(root, qid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1,
                               sort_keys=True) + "\n", encoding="utf-8")
    return qid


def ensure_question(**kwargs) -> str:
    """Create a question once per comment and requirement hash."""
    root = kwargs["root"]
    comment_id = int(kwargs["comment_id"])
    requirement = kwargs.get("requirement") or {}
    digest = str(requirement.get("requirement_hash") or "")
    for path in _dir(root).glob("*.json"):
        record = load(root, path.stem) or {}
        if record.get("comment_id") != comment_id:
            continue
        if digest and record.get("requirement_hash") != digest:
            continue
        return str(record.get("id") or path.stem)
    return create_question(**kwargs)


def url(port: int, qid: str) -> str:
    """The link a ``questions`` frame carries."""
    return f"http://{HOST}:{int(port)}/q/{qid}"


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(root, qid: str) -> dict | None:
    try:
        data = json.loads(_path(root, qid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save(root, record: dict) -> None:
    path = _path(root, record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1,
                              sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def answer_text(record: dict) -> str:
    """One short AT notification; the full answer stays in a local document."""
    return (f"【AT 答案】#{int(record.get('issue') or 0)} "
            f"r{int(record.get('revision') or 0)} 已回答；"
            f"全文：{record.get('answer_file') or '(答案文档未生成)'}")


def answer_document(root, record: dict) -> Path:
    path = Path(root) / "atserve" / "answers" / f"{record['id']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    selected = record.get("choice_labels") or []
    lines = [
        f"# AT 问卷回答 · #{int(record.get('issue') or 0)}", "",
        f"- requirement_id：{record.get('requirement_id') or record.get('thread')}",
        f"- revision：{record.get('revision') or 0}",
        f"- requirement_hash：{record.get('requirement_hash') or '(legacy)'}",
        f"- question_id：{record.get('id')}",
        f"- answered_at：{record.get('answered_at') or ''}", "",
        "## 问题", "", str(record.get("question") or ""), "",
        "## 已选选项", "",
    ]
    lines.extend([f"- {label}" for label in selected] or ["- （未选择预置选项）"])
    lines.extend(["", "## 补充说明", "", str(record.get("answer") or "（无）"), ""])
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, path)
    return path


def stale_reason(record: dict, requirement_resolver=None) -> str:
    if not record.get("requirement_id"):
        return ""
    if requirement_resolver is None:
        return "服务未接入需求版本校验"
    try:
        current = requirement_resolver(str(record.get("requirement_id")))
    except Exception as exc:  # noqa: BLE001 - fail closed and make it readable
        return f"需求版本读取失败：{type(exc).__name__}: {exc}"
    if not current:
        return "需求包不存在或不可读"
    if (current.get("revision") != record.get("revision")
            or current.get("requirement_hash") != record.get("requirement_hash")):
        return (f"问题属于 r{record.get('revision')}，当前需求已是 "
                f"r{current.get('revision')}；请使用最新链接")
    return ""


def _latest_sid(record: dict, binding_resolver=None) -> str:
    if binding_resolver is None:
        return str(record.get("sid") or "")
    binding = binding_resolver(record)
    if isinstance(binding, dict):
        return str(binding.get("session") or binding.get("sid") or "")
    return str(binding or "")


def submit(root, qid: str, answer: str, transport, *, choices=None,
           requirement_resolver=None, binding_resolver=None) -> tuple[str, str]:
    """Validate, write the answer document, then notify the latest AT."""
    record = load(root, qid)
    if record is None:
        return "failed", "question not found"
    stale = stale_reason(record, requirement_resolver)
    if stale:
        return "stale", stale
    if record.get("submitted"):
        return "already_submitted", "this question was already submitted"
    interaction = record.get("interaction") or {}
    options = interaction.get("options") or []
    by_id = {str(option.get("id")): option for option in options}
    selected = [str(value) for value in (choices or [])]
    if len(set(selected)) != len(selected) or any(value not in by_id for value in selected):
        return "invalid", "选项不存在或重复，请刷新后重试"
    if interaction.get("choice_mode") != "multiple" and len(selected) > 1:
        return "invalid", "本题只能选择一个选项"
    supplement = str(answer or "").strip()
    if not selected and not supplement:
        return "invalid", "请选择一个选项，或填写补充说明"
    if supplement and interaction.get("allow_custom") is False:
        return "invalid", "本题不允许自由补充，请只选择预置选项"
    record["sid"] = _latest_sid(record, binding_resolver)
    record["answer"] = supplement
    record["choices"] = selected
    record["choice_labels"] = [str(by_id[value].get("label") or value)
                               for value in selected]
    record["answered_at"] = _now()
    record["status"] = "answered"
    try:
        record["answer_file"] = str(answer_document(root, record).resolve())
        save(root, record)
    except OSError as exc:
        return "failed", f"答案文档写入失败：{type(exc).__name__}: {exc}"
    sid = str(record.get("sid") or "")
    if not sid:
        return "no_sid", "this question names no current AT session"
    if transport is None:
        return "no_transport", "no send transport is wired"
    try:
        transport(sid, answer_text(record))
    except Exception as exc:  # noqa: BLE001  the answer is kept; say so
        return "failed", f"{type(exc).__name__}: {exc}"
    record["submitted"] = True
    record["status"] = "submitted"
    record["submitted_at"] = _now()
    save(root, record)
    return "submitted", ""


# The stylesheet is a separate constant on purpose: it is full of `{`/`}` and
# would have to be doubled throughout if it lived inside ``_PAGE``. Inserting it
# as a *value* keeps both files readable and keeps one brace vocabulary.
_STYLE = """
:root {
  color-scheme: light dark;

  --ui-paper:        #f6f5f1;
  --ui-paper-2:      #eceae4;
  --ui-surface:      #ffffff;
  --ui-ink:          #1d2433;
  --ui-ink-strong:   #0b0f19;
  --ui-muted:        #4a5568;
  --ui-rule:         rgba(29, 36, 51, .12);
  --ui-accent:       #2f54eb;

  --ui-radius-card:  10px;
  --ui-radius-ctl:   6px;
  --ui-space-1:      4px;
  --ui-space-2:      8px;
  --ui-space-3:      12px;
  --ui-space-4:      16px;
  --ui-space-5:      20px;
  --ui-space-6:      24px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ui-paper:   #1a1a1a;
    --ui-paper-2: #222222;
    --ui-surface: #232323;
    --ui-ink:     #e8e6e3;
    --ui-ink-strong: #f4f2ef;
    --ui-muted:   #a9a6a1;
    --ui-rule:    rgba(232, 230, 227, .16);
    --ui-accent:  #7b93ff;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: var(--ui-space-6) var(--ui-space-5);
  background: var(--ui-paper);
  color: var(--ui-ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 1rem;
  line-height: 1.5;
}
main {
  max-width: 44rem;
  margin: 0 auto;
}
.eyebrow {
  margin: 0 0 var(--ui-space-2);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: .6875rem;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--ui-muted);
}
h1 {
  margin: 0 0 var(--ui-space-4);
  font-family: Georgia, "Songti SC", serif;
  font-size: 2.25rem;
  font-weight: 600;
  line-height: 1.12;
  letter-spacing: -.015em;
  color: var(--ui-ink-strong);
}
.meta {
  margin: 0 0 var(--ui-space-5);
  font-size: .9375rem;
  color: var(--ui-muted);
}
.q {
  margin: 0 0 var(--ui-space-6);
  padding: var(--ui-space-5);
  background: var(--ui-surface);
  border: 1px solid var(--ui-rule);
  border-radius: var(--ui-radius-card);
}
.q p { margin: 0; }
.notice {
  margin: 0 0 var(--ui-space-5);
  padding: var(--ui-space-4) var(--ui-space-5);
  border: 1px solid var(--ui-rule);
  border-radius: var(--ui-radius-ctl);
  color: var(--ui-ink);
}
.notice[data-state="warn"] {
  border-color: var(--ui-ink-strong);
  color: var(--ui-ink-strong);
  font-weight: 600;
}
label {
  display: block;
  margin: 0 0 var(--ui-space-2);
  font-weight: 600;
  color: var(--ui-ink-strong);
}
textarea {
  display: block;
  width: 100%;
  padding: var(--ui-space-3);
  background: var(--ui-surface);
  color: var(--ui-ink);
  border: 1px solid var(--ui-rule);
  border-radius: var(--ui-radius-ctl);
  font: inherit;
  resize: vertical;
}
.hint {
  margin: var(--ui-space-2) 0 var(--ui-space-5);
  font-size: .875rem;
  color: var(--ui-muted);
}
fieldset {
  margin: 0 0 var(--ui-space-6);
  padding: 0;
  border: 0;
}
legend {
  margin-block-end: var(--ui-space-2);
  padding: 0;
  font-size: 1.125rem;
  font-weight: 700;
}
.options { display: grid; gap: var(--ui-space-3); }
.option {
  display: grid;
  grid-template-columns: 1.5rem minmax(0, 1fr);
  align-items: start;
  gap: var(--ui-space-3);
  min-height: 48px;
  padding: var(--ui-space-4);
  background: var(--ui-surface);
  border: 1px solid var(--ui-rule);
  border-radius: var(--ui-radius-card);
}
.option input {
  width: 1.25rem;
  height: 1.25rem;
  margin: .2rem 0 0;
  accent-color: var(--ui-accent);
}
.option label { display: block; cursor: pointer; }
.option-title { display: block; font-weight: 650; }
.option-description {
  display: block;
  margin-block-start: var(--ui-space-1);
  color: var(--ui-muted);
  font-size: .9375rem;
}
.option input:focus-visible + label {
  outline: 3px solid var(--ui-accent);
  outline-offset: 4px;
  border-radius: 2px;
}
.supplement { margin-block-end: var(--ui-space-5); }
textarea { min-height: 8rem; }
.submit-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--ui-space-4);
}
.submit-row .hint { margin: 0; flex: 1 1 16rem; }
button {
  min-height: 48px;
  padding: var(--ui-space-3) var(--ui-space-5);
  background: var(--ui-accent);
  color: var(--ui-surface);
  border: 1px solid var(--ui-accent);
  border-radius: var(--ui-radius-ctl);
  font: inherit;
  font-weight: 700;
  cursor: pointer;
}
button:hover { filter: brightness(.92); }
button:focus-visible,
textarea:focus-visible {
  outline: 3px solid var(--ui-accent);
  outline-offset: 3px;
}
@media (pointer: coarse) {
  .option, button { min-height: 52px; }
}
@media (forced-colors: active) {
  .option, button { border: 1px solid ButtonText; }
}
"""

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


def make_handler(root, transport, *, requirement_resolver=None,
                 binding_resolver=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the console quiet
            return

        def _send(self, code: int, body: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802  (http.server API)
            qid = self._qid()
            record = load(root, qid) if qid else None
            if record is None:
                self._send(404, _page({"issue": ""}, "",
                                      _notice("问卷不存在。")))
                return
            stale = stale_reason(record, requirement_resolver)
            if stale:
                self._send(410, _page(record, "", _notice(
                    "本问题已过期，未提交：" + html.escape(stale), warn=True)))
                return
            if record.get("submitted"):
                self._send(200, _page(record, _answer_summary(record),
                                      _notice("本题答案已提交并回传当前 AT。")))
                return
            self._send(200, _page(record, _form(record)))

        def do_POST(self):  # noqa: N802
            from urllib.parse import parse_qs

            qid = self._qid()
            record = load(root, qid) if qid else None
            if record is None:
                self._send(404, _page({"issue": ""}, "",
                                      _notice("问卷不存在。")))
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace")
            form = parse_qs(raw, keep_blank_values=True)
            answer = str((form.get("answer") or [""])[0])
            choices = [str(value) for value in form.get("choice") or []]
            outcome, detail = submit(
                root, qid, answer, transport, choices=choices,
                requirement_resolver=requirement_resolver,
                binding_resolver=binding_resolver)
            latest = load(root, qid) or record
            if outcome == "submitted":
                self._send(200, _page(latest, _answer_summary(latest),
                                      _notice("已确认，答案已回传当前 AT。")))
            elif outcome == "stale":
                self._send(410, _page(record, "", _notice(
                    "本问题已过期，答案未保存、未回传：" + html.escape(detail),
                    warn=True)))
            elif outcome == "invalid":
                record["answer"] = answer
                record["choices"] = choices
                self._send(422, _page(record, _form(record),
                                      _notice(html.escape(detail), warn=True)))
            elif outcome == "already_submitted":
                self._send(409, _page(latest, _answer_summary(latest), _notice(
                    "本题已经提交，不重复回传。", warn=True)))
            elif outcome == "no_sid":
                self._send(409, _page(latest, "", _notice(
                    "答案文档已保存，但没有回传：当前没有可用 AT 会话。", warn=True)))
            elif outcome == "no_transport":
                self._send(409, _page(latest, "", _notice(
                    "答案文档已保存，但没有回传：服务未接入发送通道。", warn=True)))
            else:
                self._send(502, _page(latest, "", _notice(
                    "答案未回传：" + html.escape(detail), warn=True)))

        def do_HEAD(self):  # noqa: N802
            self._send(200, "")

        def _qid(self) -> str:
            path = self.path.split("?", 1)[0]
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0] == "q":
                return parts[1]
            if len(parts) == 1:
                return parts[0]
            return ""

    return Handler


def _unquote(value: str) -> str:
    from urllib.parse import unquote_plus
    return unquote_plus(value)


class _Server(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` that refuses a port somebody else already holds.

    ``HTTPServer.allow_reuse_address`` is **1** by default, and on Windows that
    is not the harmless "skip TIME_WAIT" it sounds like: it lets a second
    process bind a port that is already listening. Two ``at_serve`` instances
    then both exit 0, the second silently takes over, and every link handed out
    by the first answers 404 - while ``atserve.pid`` names whichever wrote
    last. A link nobody answers is the failure this service exists to avoid, so
    the option is turned off and the bind error is the only outcome.
    """

    allow_reuse_address = False


def serve(*, root, port: int = DEFAULT_PORT, transport=None,
          requirement_resolver=None, binding_resolver=None) -> ThreadingHTTPServer:
    """Bind loopback now and return the server, unserved."""
    base = Path(root) / "atserve"
    base.mkdir(parents=True, exist_ok=True)
    server = _Server((HOST, int(port)), make_handler(
        root, transport, requirement_resolver=requirement_resolver,
        binding_resolver=binding_resolver))
    (base / "atserve.pid").write_text(str(os.getpid()), encoding="utf-8")
    return server


def _deployment_resolvers(root):
    from . import requirements
    from . import session_ledger

    def requirement(requirement_id):
        return requirements.current(requirements.load_projection(root), requirement_id)

    def binding(record):
        database = session_ledger.load_issue_sessions()
        for key in record.get("binding_keys") or [record.get("thread")]:
            entry = database.get(str(key)) or {}
            at = entry.get("at") if isinstance(entry, dict) else {}
            if isinstance(at, dict) and at.get("session"):
                return at
        return {}
    return requirement, binding


def run(*, root, port: int = DEFAULT_PORT, transport=None,
        requirement_resolver=None, binding_resolver=None) -> int:
    """Serve until interrupted; wire live requirement and AT binding lookups."""
    base = Path(root) / "atserve"
    if requirement_resolver is None or binding_resolver is None:
        default_requirement, default_binding = _deployment_resolvers(root)
        requirement_resolver = requirement_resolver or default_requirement
        binding_resolver = binding_resolver or default_binding
    try:
        server = serve(root=root, port=port, transport=transport,
                       requirement_resolver=requirement_resolver,
                       binding_resolver=binding_resolver)
    except OSError as exc:
        print(f"at_serve: 无法绑定 {HOST}:{port} - {exc}", file=sys.stderr)
        return 1
    print(f"at_serve: http://{HOST}:{port}  root={base}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            (base / "atserve.pid").unlink()
        except OSError:
            pass
    return 0


def _real_transport(sid: str, text: str) -> bool:
    """The repo's own console client, wired only by ``--allow-send``."""
    from . import direct
    direct.send(sid, text)
    return True


def main(argv=None) -> int:
    import argparse

    from . import agent_config
    from . import config as config_mod
    from . import store as store_mod

    parser = argparse.ArgumentParser(prog="python -m super_agent.at_serve")
    parser.add_argument("--root", default="", help="state root (default: the store root)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--allow-send", action="store_true",
                        help="send the answer to the AT console (off by default)")
    args = parser.parse_args(argv)
    # Resolve the root the same way ``cli._build`` does, so the service reads
    # the files the engine wrote instead of a look-alike directory beside them.
    root, _note = store_mod.choose_root(config_mod.state_root(
        config_mod.load(overrides=agent_config.overrides())))
    if args.root:
        root = Path(args.root)
    return run(root=root, port=args.port,
               transport=_real_transport if args.allow_send else None)


if __name__ == "__main__":
    raise SystemExit(main())
