from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from super_agent import at_serve


class RequirementQuestions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.requirement = {"requirement_id": "owner/demo#1", "revision": 1,
                            "requirement_hash": "sha256:" + "a" * 64}

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, requirement=None):
        return at_serve.create_question(
            root=self.root, thread="owner/demo#1", issue=1, comment_id=100,
            sid="session_old", question="one question",
            requirement=requirement or self.requirement,
            binding_keys=["owner/demo#1", "demo#1"])

    def test_stale_answer_is_neither_saved_nor_sent(self):
        qid = self.create()
        sent = []
        current = dict(self.requirement, revision=2,
                       requirement_hash="sha256:" + "b" * 64)
        outcome, detail = at_serve.submit(
            self.root, qid, "old answer", lambda sid, text: sent.append((sid, text)),
            requirement_resolver=lambda rid: current,
            binding_resolver=lambda record: "session_new")
        self.assertEqual(outcome, "stale")
        self.assertIn("r2", detail)
        self.assertEqual(sent, [])
        record = at_serve.load(self.root, qid)
        self.assertEqual(record["answer"], "")
        self.assertFalse(record["submitted"])

    def test_current_answer_goes_only_to_the_latest_at_binding(self):
        qid = self.create()
        sent = []
        outcome, detail = at_serve.submit(
            self.root, qid, "current answer", lambda sid, text: sent.append((sid, text)),
            requirement_resolver=lambda rid: self.requirement,
            binding_resolver=lambda record: {"session": "session_new"})
        self.assertEqual(outcome, "submitted", detail)
        self.assertEqual([sid for sid, text in sent], ["session_new"])
        self.assertNotIn("current answer", sent[0][1])
        self.assertEqual(len(sent[0][1].splitlines()), 1)
        record = at_serve.load(self.root, qid)
        self.assertEqual(record["sid"], "session_new")
        self.assertEqual(record["status"], "submitted")
        self.assertIn(record["answer_file"], sent[0][1])
        document = Path(record["answer_file"]).read_text(encoding="utf-8")
        self.assertIn("current answer", document)

    def test_versioned_question_fails_closed_without_a_resolver(self):
        qid = self.create()
        outcome, detail = at_serve.submit(
            self.root, qid, "answer", lambda sid, text: None)
        self.assertEqual(outcome, "stale")
        self.assertIn("未接入", detail)

    def test_successive_revisions_have_independent_question_state(self):
        first = self.create()
        at_serve.submit(
            self.root, first, "one", lambda sid, text: None,
            requirement_resolver=lambda rid: self.requirement,
            binding_resolver=lambda record: "session_one")
        newer = dict(self.requirement, revision=2,
                     requirement_hash="sha256:" + "b" * 64)
        second = self.create(newer)
        self.assertNotEqual(first, second)
        self.assertTrue(at_serve.load(self.root, first)["submitted"])
        self.assertFalse(at_serve.load(self.root, second)["submitted"])
        self.assertEqual(at_serve.load(self.root, second)["revision"], 2)

    def test_ensure_question_is_idempotent_per_comment_and_hash(self):
        first = self.create()
        second = at_serve.ensure_question(
            root=self.root, thread="owner/demo#1", issue=1, comment_id=100,
            sid="session_new", question="rewritten display",
            requirement=self.requirement)
        self.assertEqual(first, second)
        self.assertEqual(len(list((self.root / "atserve" / "questions").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
