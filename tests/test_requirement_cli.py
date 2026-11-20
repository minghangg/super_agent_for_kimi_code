from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from super_agent import requirement_cli


class CommentPaging(unittest.TestCase):
    def test_live_reader_uses_real_github_pagination_and_normalises_rows(self):
        pages = [
            [{"id": 100, "body": "one", "user": {"login": "at"},
              "created_at": "then"}] * 100,
            [{"id": 200, "body": "two", "user": {"login": "at"},
              "created_at": "later"}],
        ]
        ctx = SimpleNamespace(fetch={})
        with mock.patch.object(requirement_cli.issue_cli, "_call",
                               side_effect=pages) as call:
            rows = requirement_cli._comments(ctx, "owner/demo", 7)
        self.assertEqual(len(rows), 101)
        self.assertEqual(rows[-1], {"id": 200, "body": "two", "author": "at",
                                    "created_at": "later"})
        paths = [args[0][1] for args in call.call_args_list]
        self.assertEqual(paths[0], "/repos/owner/demo/issues/7/comments?per_page=100&page=1")
        self.assertEqual(paths[1], "/repos/owner/demo/issues/7/comments?per_page=100&page=2")


if __name__ == "__main__":
    unittest.main()
