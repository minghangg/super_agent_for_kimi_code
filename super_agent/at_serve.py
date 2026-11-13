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
                    sid: str, question: str) -> str:
    """File one question and return its id. Needs no running service.

    The id is random rather than sequential: the link is the only thing that
    distinguishes one question from another on a shared machine, and a guessable
    one would let a stray browser tab answer somebody else's AT.
    """
    qid = uuid.uuid4().hex[:12]
    record = {"id": qid, "thread": str(thread), "issue": int(issue),
              "comment_id": int(comment_id), "sid": str(sid or ""),
              "question": str(question or ""), "answer": "",
              "submitted": False, "created_at": _now()}
    path = _path(root, qid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1,
                               sort_keys=True) + "\n", encoding="utf-8")
    return qid


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
    """What the AT receives. One line of identity, then the answer verbatim."""
    return (f"【问卷回答】#{int(record.get('issue') or 0)} "
            f"({record.get('thread') or ''}, cid {record.get('comment_id') or 0})"
            f"\n{record.get('answer') or ''}")


def submit(root, qid: str, answer: str, transport) -> tuple[str, str]:
    """Store the answer, then send it back to the AT.

    ``(outcome, detail)`` where ``outcome`` is one of::

        "submitted"   the transport ran and did not raise
        "no_sid"      the question carries no AT session to answer
        "no_transport" no transport was injected - the answer is stored only
        "failed"      the transport raised; the answer is kept, nothing claims
                      it arrived

    The answer is written **before** the send on purpose: a reply that reached
    the AT and was lost here would leave the AT talking about an answer no file
    can produce.
    """
    record = load(root, qid)
    if record is None:
        return "failed", "question not found"
    record["answer"] = str(answer or "")
    record["answered_at"] = _now()
    save(root, record)
    sid = str(record.get("sid") or "")
    if not sid:
        return "no_sid", "this question names no AT session"
    if transport is None:
        return "no_transport", "no send transport is wired"
    try:
        transport(sid, answer_text(record))
    except Exception as exc:  # noqa: BLE001  the answer is kept; say so
        return "failed", f"{type(exc).__name__}: {exc}"
    record["submitted"] = True
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
button {
  padding: var(--ui-space-3) var(--ui-space-5);
  background: transparent;
  color: var(--ui-accent);
  border: 1px solid var(--ui-accent);
  border-radius: var(--ui-radius-ctl);
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
button:hover { background: var(--ui-paper-2); }
button:focus-visible,
textarea:focus-visible {
  outline: 2px solid var(--ui-accent);
  outline-offset: 2px;
}
"""

# **No `box-shadow` anywhere in this sheet, and it is deliberate.** The report
# this style follows states the rule plainly: a shadow pulls the page back into
# "admin console". Depth comes from one 1px `--ui-rule` hairline instead.
#
# **No `all: unset`.** It is the shorthand that silently swallows the focus ring;
# the controls here get their look from explicit properties, and the focus ring
# is `outline`, not a shadow, so it survives Forced Colors Mode.
#
# **No `touch-action`.** The page has one scrolling textarea and no gestures, so
# the property would be cargo cult; if a gesture ever lands here it must be
# per-axis (`pan-y`), never `touch-action: none`.
_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>AT 问卷 · #{issue}</title>
<style>{style}</style>
</head>
<body>
<main>
<p class="eyebrow">{project} · 问卷</p>
<h1>AT 有疑问 · #{issue}</h1>
<p class="meta"><strong>{thread}</strong>（cid {cid}）</p>
<section class="q">
<p>{question}</p>
</section>
{notice}
{body}
</main>
</body>
</html>
"""

_FORM = """<form method="post" action="/q/{qid}">
<label for="answer">你的回答</label>
<textarea id="answer" name="answer" rows="6" required aria-describedby="hint"></textarea>
<p id="hint" class="hint">提交后答案会回传给 AT。</p>
<p><button type="submit">提交答案</button></p>
</form>"""


def _notice(text: str, *, warn: bool = False) -> str:
    """One status line. **Colour is never the signal**: the border and the
    weight carry it, and the words say it outright."""
    state = ' data-state="warn"' if warn else ""
    return f'<p class="notice"{state}>{text}</p>'


def _page(record: dict, body: str, notice: str = "") -> str:
    thread = str(record.get("thread") or "")
    return _PAGE.format(
        style=_STYLE,
        project=html.escape(thread.split("#", 1)[0] or "AT"),
        issue=html.escape(str(record.get("issue") or "")),
        thread=html.escape(thread),
        cid=html.escape(str(record.get("comment_id") or "")),
        question=html.escape(str(record.get("question") or "")),
        notice=notice,
        body=body)


def _form(record: dict) -> str:
    return _FORM.format(qid=html.escape(str(record.get("id") or "")))


def make_handler(root, transport):
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
            if record.get("submitted"):
                self._send(200, _page(
                    record, "<pre class=\"answer\">"
                            + html.escape(str(record.get("answer") or ""))
                            + "</pre>",
                    _notice("本题答案已提交。")))
                return
            self._send(200, _page(record, _form(record)))

        def do_POST(self):  # noqa: N802
            qid = self._qid()
            record = load(root, qid) if qid else None
            if record is None:
                self._send(404, _page({"issue": ""}, "",
                                      _notice("问卷不存在。")))
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace")
            answer = ""
            for part in raw.split("&"):
                key, _, value = part.partition("=")
                if key == "answer":
                    answer = _unquote(value)
            outcome, detail = submit(root, qid, answer, transport)
            if outcome == "submitted":
                self._send(200, _page(
                    record, "", _notice("已提交，答案已回传 AT。")))
            elif outcome == "no_sid":
                self._send(409, _page(
                    record, "",
                    _notice("答案已保存，但没有回传：本题没有登记 AT 会话。",
                            warn=True)))
            elif outcome == "no_transport":
                self._send(409, _page(
                    record, "",
                    _notice("答案已保存，但没有回传：服务未接入发送通道。",
                            warn=True)))
            else:
                self._send(502, _page(
                    record, "",
                    _notice("答案已保存，但回传失败：" + html.escape(detail),
                            warn=True)))

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


def serve(*, root, port: int = DEFAULT_PORT, transport=None) -> ThreadingHTTPServer:
    """Bind ``127.0.0.1:port`` **now** and return the server, unserved.

    Binding is part of construction: an occupied port must fail before anyone
    is handed a link, because a link that answers nothing is worse than an
    error the operator can read. The caller runs ``serve_forever`` and closes.

    The pid file is written here and removed by :func:`run`, which is the entry
    that owns the process lifetime.
    """
    base = Path(root) / "atserve"
    base.mkdir(parents=True, exist_ok=True)
    server = _Server((HOST, int(port)), make_handler(root, transport))
    (base / "atserve.pid").write_text(str(os.getpid()), encoding="utf-8")
    return server


def run(*, root, port: int = DEFAULT_PORT, transport=None) -> int:
    """Serve until interrupted; return an exit code (0 clean, 1 bind failure)."""
    base = Path(root) / "atserve"
    try:
        server = serve(root=root, port=port, transport=transport)
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
    root, _note = store_mod.choose_root(
        config_mod.state_root(config_mod.load(None, {})))
    if args.root:
        root = Path(args.root)
    return run(root=root, port=args.port,
               transport=_real_transport if args.allow_send else None)


if __name__ == "__main__":
    raise SystemExit(main())
