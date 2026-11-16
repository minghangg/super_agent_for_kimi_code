"""``<!-- session: <key> -->``: the comment's own declaration of who wrote it.

Every role shares one GitHub token, so an author login cannot tell a real
verdict from a forgery. ``gh_issue.py comment --session-key`` appends one
wrapped token on **its own line**, and :func:`markers.read_session_token` reads
it back into ``comment["session_key"]``.

Three properties are pinned here, and each is a counter-example the obvious
implementation gets wrong:

* the token is on the **last line**, not the first, so a parser that only reads
  ``first_line`` (like every verdict parser in this module) finds nothing -
  the reader must scan the body;
* a body that quotes another comment carries that comment's token too, so
  **the last occurrence wins**; taking the first lets a quotation claim an
  identity;
* no token means ``""`` - never "the author looked plausible", which is the
  guess the engine's fail-closed checks exist to remove.

Run: ``python -m pytest tests/test_session_token.py -q``
"""
from __future__ import annotations

import unittest

from super_agent import github_source, markers

READY = "[PM] 可关单 #1 <!-- verdict: ready -->"


class ReadSessionToken(unittest.TestCase):
    def test_a_trailing_marker_is_read(self):
        self.assertEqual(
            markers.read_session_token(READY + "\n<!-- session: pm-demo -->"),
            "pm-demo")

    def test_no_marker_means_empty_not_guessed(self):
        self.assertEqual(markers.read_session_token(READY), "")
        self.assertEqual(markers.read_session_token(""), "")

    def test_the_last_marker_wins(self):
        body = ("see the earlier comment:\n"
                "<!-- session: pm-someone-else -->\n"
                + READY + "\n<!-- session: pm-demo -->")
        self.assertEqual(markers.read_session_token(body), "pm-demo")

    def test_the_marker_may_sit_on_any_line(self):
        body = "<!-- session: qa-demo -->\n" + READY
        self.assertEqual(markers.read_session_token(body), "qa-demo")

    def test_a_key_with_dots_and_underscores_survives(self):
        self.assertEqual(
            markers.read_session_token("<!-- session: qa_lwa.ISS-87 -->"),
            "qa_lwa.ISS-87")

    def test_a_malformed_marker_is_not_read(self):
        # No closing ``-->``, a space inside the key, or an empty key: none of
        # them is a declaration, and guessing one is worse than reading none.
        for body in ("<!-- session: pm-demo",
                     "<!-- session: pm demo -->",
                     "<!-- session:  -->"):
            self.assertEqual(markers.read_session_token(body), "", body)

    def test_the_token_does_not_disturb_the_verdict_line(self):
        """The session token is a **separate** line for this reason."""
        body = READY + "\n<!-- session: pm-demo -->"
        self.assertEqual(markers.parse_pm_verdict(body)["state"], "ready")
        self.assertTrue(markers.parse_pm_verdict(body)["structured"])

    def test_the_same_line_would_hide_the_verdict(self):
        """Counter-example: appending to the verdict line breaks the parse.

        Documented so the separate line cannot be "tidied up" into one line by
        a later change: every verdict token is anchored at the end of its line.
        """
        body = READY + " <!-- session: pm-demo -->"
        verdict = markers.parse_pm_verdict(body)
        self.assertEqual(verdict["state"], "")
        self.assertEqual(markers.read_session_token(body), "pm-demo")


class SourceCarriesTheKey(unittest.TestCase):
    ROW = {"id": 9100, "body": READY + "\n<!-- session: pm-demo -->",
           "user": {"login": "shared"}, "created_at": ""}

    def test_the_live_normaliser_reads_the_session_token(self):
        out = github_source.LiveSource._normalise([self.ROW], 1)
        self.assertEqual(out[0]["session_key"], "pm-demo")

    def test_the_live_reader_reads_the_session_token(self):
        live = github_source.LiveSource("owner/demo", token="")
        live._get = lambda path, query=None: [self.ROW]
        self.assertEqual(live.comments(1)[0]["session_key"], "pm-demo")

    def test_the_latest_page_reader_reads_the_session_token(self):
        live = github_source.LiveSource("owner/demo", token="")
        live._get = lambda path, query=None: [self.ROW]
        self.assertEqual(live.paged(1)[0]["session_key"], "pm-demo")

    def test_no_token_means_empty_in_every_reader(self):
        row = dict(self.ROW, body=READY)
        self.assertEqual(
            github_source.LiveSource._normalise([row], 1)[0]["session_key"], "")
        live = github_source.LiveSource("owner/demo", token="")
        live._get = lambda path, query=None: [row]
        self.assertEqual(live.comments(1)[0]["session_key"], "")
        self.assertEqual(live.paged(1)[0]["session_key"], "")

    def test_the_fixture_backend_reads_it_too(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.json"
            path.write_text(json.dumps({"issues": {"1": {"comments": [
                {"id": 9100, "body": self.ROW["body"], "author": "shared",
                 "created_at": ""}]}}}), encoding="utf-8")
            source = github_source.FixtureSource(path)
            self.assertEqual(source.comments(1)[0]["session_key"], "pm-demo")


if __name__ == "__main__":
    unittest.main()
