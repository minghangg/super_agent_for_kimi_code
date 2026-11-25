"""#134 P0-2: the CLI write path must fail loudly on transport errors.

``request_or_exit`` returns a parsed body only when a request actually settled.
A proxy that is down, a DNS failure or a timeout has ``_status == 0``, and
treating that as success made ``cmd_comment`` print ``commented … id=None`` and
write a ledger row for a comment nobody sent.

Run: ``python -m pytest tests/test_github_write_client.py -q``
"""
from __future__ import annotations

import unittest
from unittest import mock

from super_agent.github import write_client


class WritePathFailsLoudly(unittest.TestCase):
    def call(self, status, body=None, error=""):
        with mock.patch.object(write_client, "request",
                               return_value={"_status": status, "body": body,
                                             "error": error}):
            return write_client.request_or_exit("https://x/y", method="POST")

    def test_a_transport_failure_raises_instead_of_returning_empty(self):
        with self.assertRaises(SystemExit):
            self.call(0, None, "ClientProxyConnectionError")

    def test_a_timeout_raises(self):
        with self.assertRaises(SystemExit):
            self.call(0, None, "timeout")

    def test_an_http_error_still_raises(self):
        with self.assertRaises(SystemExit):
            self.call(422, None, "bad")

    def test_a_success_still_returns_the_body(self):
        self.assertEqual(self.call(201, {"id": 5}), {"id": 5})

    def test_the_transport_message_names_the_failure(self):
        with self.assertRaises(SystemExit) as caught:
            self.call(0, None, "ClientProxyConnectionError")
        self.assertIn("请求未完成", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
