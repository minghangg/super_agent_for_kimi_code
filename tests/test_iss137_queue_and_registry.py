"""Queue merge and duplicate registration (#124 N5, N12).

Both are rules the suite named but never pinned, so a change could remove them
and stay green.

**N5.** ``test_iss130_batch_round.py`` exercised a *new* thread's first put. The
merge branch - a second put for a thread already in the queue - had no assertion,
so replacing the batch instead of merging would have dropped the first batch's
unconsumed rows with every test passing. Losing ``id 1`` there is permanent: the
cursor moved past it and nothing reads it again.

**N12.** ``Runner.register`` raises on a duplicate name, and nothing asserted it.
Returning the existing component instead would have been invisible - and that
behaviour is worse than a refusal, because the caller believes it replaced the
body while the old one keeps running.

Run: ``python -m pytest tests/test_iss137_queue_and_registry.py -q``
"""
from __future__ import annotations

import unittest

from super_agent import runner
from super_agent.batch import CollectedBatch, ScanPolicy
from super_agent.batch_queue import BoundedBatchQueue


class SecondBatchMustNotDropTheFirst(unittest.TestCase):
    """A second put for one thread merges; it never replaces."""

    def test_second_batch_keeps_the_unconsumed_rows(self):
        queue = BoundedBatchQueue(ScanPolicy(max_batches=5, max_batch_rows=100,
                                             max_issue_rows=100))
        first = CollectedBatch("a", [{"id": 1}, {"id": 2}], complete=True)
        queue.put(first)
        second = CollectedBatch("a", [{"id": 2}, {"id": 3}], complete=True)
        queue.put(second)
        held = queue.get("a")
        self.assertEqual([row["id"] for row in held.rows], [1, 2, 3],
                         "第二批不得覆盖第一批：id 1 会永久丢失")

    def test_the_slot_is_not_double_counted(self):
        queue = BoundedBatchQueue(ScanPolicy(max_batches=1, max_batch_rows=100,
                                             max_issue_rows=100))
        queue.put(CollectedBatch("a", [{"id": 1}], complete=True))
        queue.put(CollectedBatch("a", [{"id": 2}], complete=True))
        self.assertEqual(queue.size(), 1, "同一 thread 只占一个槽")


class DuplicateRegistrationIsRefused(unittest.TestCase):
    """A duplicate name is refused, never silently reused."""

    def test_a_second_register_of_the_same_name_raises(self):
        rn = runner.Runner()
        rn.register("readapi", lambda: None, kind=runner.THREAD)
        with self.assertRaises(ValueError):
            rn.register("readapi", lambda: None, kind=runner.THREAD)

    def test_the_first_component_is_untouched_after_the_refusal(self):
        rn = runner.Runner()
        first = rn.register("readapi", lambda: None, kind=runner.THREAD)
        with self.assertRaises(ValueError):
            rn.register("readapi", lambda: None, kind=runner.THREAD)
        self.assertIs(rn.component("readapi"), first,
                      "拒绝之后已有组件必须原样保留")


if __name__ == "__main__":
    unittest.main()
