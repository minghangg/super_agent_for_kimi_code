from __future__ import annotations

import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from super_agent import at_serve


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class InteractivePage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.requirement = {
            "requirement_id": "owner/demo#1", "revision": 1,
            "requirement_hash": "sha256:" + "a" * 64,
            "objective": "choose a delivery boundary",
            "interaction": {
                "choice_mode": "single", "allow_custom": True,
                "options": [
                    {"id": "core", "label": "先交付核心流程",
                     "description": "不包含管理界面"},
                    {"id": "full", "label": "一次交付全部",
                     "description": "范围更大"},
                ]},
        }
        self.current = dict(self.requirement)
        self.received = []
        self.port = free_port()
        self.server = at_serve.serve(
            root=self.root, port=self.port,
            transport=lambda sid, text: self.received.append((sid, text)),
            requirement_resolver=lambda rid: self.current,
            binding_resolver=lambda record: "session_at")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def create(self):
        qid = at_serve.create_question(
            root=self.root, thread="owner/demo#1", issue=1, comment_id=100,
            sid="session_old", question="先交付哪个边界？",
            requirement=self.requirement)
        return qid, at_serve.url(self.port, qid)

    def test_option_and_supplement_submit_to_short_message_and_document(self):
        qid, link = self.create()
        page = urllib.request.urlopen(link, timeout=5).read().decode("utf-8")
        self.assertIn('type="radio"', page)
        self.assertIn('name="choice"', page)
        self.assertIn("先交付核心流程", page)
        self.assertIn("不包含管理界面", page)
        self.assertIn("确认并回答 AT", page)
        self.assertIn("<fieldset>", page)
        self.assertIn("<legend>", page)

        data = urllib.parse.urlencode({"choice": "core", "answer": "补充一条约束"}).encode()
        response = urllib.request.urlopen(link, data=data, timeout=5)
        body = response.read().decode("utf-8")
        self.assertIn("已确认", body)
        self.assertIn("先交付核心流程", body)
        self.assertEqual(len(self.received), 1)
        sid, message = self.received[0]
        self.assertEqual(sid, "session_at")
        self.assertEqual(len(message.splitlines()), 1)
        self.assertNotIn("补充一条约束", message)
        record = at_serve.load(self.root, qid)
        self.assertEqual(record["choices"], ["core"])
        document = Path(record["answer_file"]).read_text(encoding="utf-8")
        self.assertIn("先交付核心流程", document)
        self.assertIn("补充一条约束", document)

    def test_unknown_option_is_a_422_without_document_or_notification(self):
        qid, link = self.create()
        data = urllib.parse.urlencode({"choice": "injected"}).encode()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(link, data=data, timeout=5)
        self.assertEqual(caught.exception.code, 422)
        self.assertIn("选项不存在", caught.exception.read().decode("utf-8"))
        self.assertEqual(self.received, [])
        record = at_serve.load(self.root, qid)
        self.assertEqual(record["answer"], "")
        self.assertNotIn("answer_file", record)

    def test_old_revision_page_is_gone_instead_of_a_live_form(self):
        _qid, link = self.create()
        self.current = dict(self.requirement, revision=2,
                            requirement_hash="sha256:" + "b" * 64)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(link, timeout=5)
        self.assertEqual(caught.exception.code, 410)
        body = caught.exception.read().decode("utf-8")
        self.assertIn("已过期", body)
        self.assertNotIn("确认并回答 AT", body)


if __name__ == "__main__":
    unittest.main()
