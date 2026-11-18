"""L1 tests for the wire-side context read (#94).

The value that judges the dispatch line must carry **when it was produced** and
**whether its window is known**. These tests pin the three ways that goes
wrong: a stale record read as current, a fresh record from another source
washing an old value clean, and a record split across a block boundary.

Run: ``python -m pytest tests/test_ctx_wire.py -q``
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from super_agent import direct

SID = "session_iss94_probe"


class WireFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iss94_wire_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dir = self.root / "wd_probe" / SID / "agents" / "main"
        self.dir.mkdir(parents=True)
        self.wire = self.dir / "wire.jsonl"
        patcher = mock.patch.dict(os.environ, {"SESS_WIRE_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(direct._WINDOWS_CACHE.clear)

    def write(self, *records: dict) -> None:
        self.wire.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    @staticmethod
    def usage(*, age_s: float = 0, model: str = "kimi-code/k3",
              other: int = 1000, out: int = 100,
              cache_read: int = 2000, cache_create: int = 0) -> dict:
        return {"type": "usage.record", "agentId": "main", "model": model,
                "usage": {"inputOther": other, "output": out,
                          "inputCacheRead": cache_read,
                          "inputCacheCreation": cache_create},
                "usageScope": "turn",
                "time": int((time.time() - age_s) * 1000)}


class FreshRecord(WireFixture):
    def test_four_addends_include_output(self):
        self.write(self.usage())
        got = direct.wire_ctx(SID)
        self.assertEqual(got["context_tokens_raw"], 3100)     # 1000+2000+0+100
        self.assertEqual(got["ctx_state"], "live")
        self.assertLessEqual(got["age_s"], 2)

    def test_session_scope_records_are_skipped(self):
        rec = self.usage()
        rec["usageScope"] = "session"
        rec["usage"] = {"inputOther": 999999, "output": 0,
                        "inputCacheRead": 0, "inputCacheCreation": 0}
        self.write(rec, self.usage())
        self.assertEqual(direct.wire_ctx(SID)["context_tokens_raw"], 3100)

    def test_unknown_window_is_absent_not_a_guessed_default(self):
        self.write(self.usage(model="someone/not-in-config"))
        got = direct.wire_ctx(SID)
        self.assertEqual(got["ctx_state"], "absent")
        self.assertIsNone(got["max_context_tokens"])
        self.assertIn("window=unknown", got["_source"])
        self.assertEqual(got["context_tokens_raw"], 3100)      # 值不丢


class Staleness(WireFixture):
    def test_old_value_is_stale_and_keeps_its_number(self):
        self.write(self.usage(age_s=1234))
        got = direct.wire_ctx(SID)
        self.assertEqual(got["ctx_state"], "stale")
        self.assertEqual(got["context_tokens_raw"], 3100)
        self.assertGreaterEqual(got["age_s"], 1230)

    def test_a_fresh_turn_recorded_never_washes_a_stale_usage_record(self):
        self.write(self.usage(age_s=1234),
                   {"type": "token_counting.turn_recorded", "agentId": "main",
                    "turnId": 9, "tokens": 999999,
                    "time": int(time.time() * 1000)})
        got = direct.wire_ctx(SID)
        self.assertEqual(got["ctx_state"], "stale")
        self.assertEqual(got["context_tokens_raw"], 3100)

    def test_turn_recorded_is_the_fifth_route_only(self):
        session_level = self.usage()
        session_level["usageScope"] = "session"
        self.write(session_level,
                   {"type": "token_counting.turn_recorded", "agentId": "main",
                    "turnId": 9, "tokens": 4242,
                    "time": int(time.time() * 1000)})
        got = direct.wire_ctx(SID)
        self.assertEqual(got["context_tokens_raw"], 4242)
        self.assertIn("turn_recorded", got["_source"])
        # 窗口取自同会话最近一条 usage.record 的模型；该记录本身不带模型名。
        self.assertEqual(got["max_context_tokens"], 1048576)
        self.assertEqual(got["ctx_state"], "live")

    def test_turn_recorded_without_a_model_has_no_window(self):
        self.write({"type": "token_counting.turn_recorded", "agentId": "main",
                    "turnId": 9, "tokens": 4242,
                    "time": int(time.time() * 1000)})
        got = direct.wire_ctx(SID)
        self.assertEqual(got["context_tokens_raw"], 4242)
        self.assertEqual(got["ctx_state"], "absent")     # 窗口未知 ⇒ 不可判
        self.assertIsNone(got["max_context_tokens"])


class TailRead(WireFixture):
    def test_a_record_split_across_blocks_is_still_found(self):
        """The tail reader glues a line whose head sits in the older chunk."""
        pad = {"type": "text", "pad": "x" * 200}
        self.write(*([pad] * 40), self.usage())
        size = self.wire.stat().st_size
        for block in (size, size - 1, 97, 64):
            with self.subTest(block=block):
                direct._WINDOWS_CACHE.clear()
                got = direct.wire_ctx(SID)
                self.assertEqual(got["context_tokens_raw"], 3100)
                self.assertEqual(got["ctx_state"], "live")

    def test_no_record_at_all_is_absent(self):
        self.write({"type": "text", "t": "noise"})
        got = direct.wire_ctx(SID)
        self.assertEqual(got["ctx_state"], "absent")
        self.assertIsNone(got["value_ts"])

    def test_missing_wire_file_is_absent(self):
        got = direct.wire_ctx("session_does_not_exist")
        self.assertEqual(got["ctx_state"], "absent")
        self.assertIn("no_file", got["_source"])


class WindowTable(WireFixture):
    def test_quoted_and_bare_sections_both_parse(self):
        cfg = self.root / "config.toml"
        cfg.write_text(
            '[models."vendor/quoted"]\nmax_context_size = 1000\n\n'
            "[models.bare-name]\nmax_context_size = 2000\n\n"
            '[models."vendor/inner"]\nmodel = "inner-name"\n'
            "max_context_size = 3000\n",
            encoding="utf-8")
        table = direct._model_windows(cfg)
        self.assertEqual(table.get("vendor/quoted"), 1000)
        self.assertEqual(table.get("bare-name"), 2000)
        self.assertEqual(table.get("inner-name"), 3000)
        self.assertIsNone(table.get("nobody/knows"))


class StatusContract(WireFixture):
    def test_the_raw_carrier_is_neither_a_template_key_nor_a_judging_input(self):
        self.assertNotIn("context_tokens_raw", direct.STATUS_KEYS)
        self.assertIn("context_tokens_raw", direct.NON_JUDGE_KEYS)
        for key in ("ctx_state", "value_ts", "age_s"):
            self.assertIn(key, direct.status_template())

    def test_a_dead_session_is_absent_not_degraded(self):
        dead = {"_err": 'http=200 {"code": 40401, "msg": "does not exist"}'}
        with mock.patch.object(direct, "session_info", lambda sid: dead):
            out = direct.status("session_00000000-dead-beef-0000-000000000000")
        self.assertEqual(out.get("ctx_state"), "absent")
        self.assertEqual(out.get("_source"), "absent(http=40401)")
        self.assertIsNone(out.get("context_usage"))

    def test_stale_status_hides_every_judging_field(self):
        with mock.patch.object(direct, "session_info", lambda sid: {}), \
                mock.patch.object(direct, "is_new", lambda: True), \
                mock.patch.object(direct, "_ws_status", lambda sid: {}), \
                mock.patch.object(direct, "wire_ctx",
                                  lambda sid: {"ctx_state": "stale",
                                               "value_ts": 1, "age_s": 1234,
                                               "context_tokens_raw": 297329,
                                               "max_context_tokens": 1000000,
                                               "_source": "wire(stale)"}):
            out = direct.status("sid-1")
        self.assertEqual(out["ctx_state"], "stale")
        self.assertEqual(out["context_tokens_raw"], 297329)
        for key in ("context_tokens", "max_context_tokens", "context_usage"):
            self.assertIsNone(out[key], key)

    def test_a_cache_record_cannot_carry_a_judging_number(self):
        """The cache has no value time, so its number is never judgeable."""
        sid = "sid-cache"
        cache = self.root / "c.json"
        cache.write_text(json.dumps({"sessions": {sid: {
            "ts": int(time.time()),
            "status": {"busy": False, "context_tokens": 297329,
                       "max_context_tokens": 1000000}}}}), encoding="utf-8")
        absent = {"ctx_state": "absent", "value_ts": None, "age_s": None,
                  "context_tokens_raw": None, "max_context_tokens": None,
                  "_source": "wire:no_file"}
        with mock.patch.object(direct, "session_info", lambda s: {}), \
                mock.patch.object(direct, "is_new", lambda: True), \
                mock.patch.object(direct, "_ws_status", lambda s: {}), \
                mock.patch.object(direct, "wire_ctx", lambda s: absent):
            out = direct.status(sid, cache_file=cache)
        self.assertEqual(out["ctx_state"], "absent")
        for key in ("context_tokens", "max_context_tokens", "context_usage"):
            self.assertIsNone(out[key], key)
        self.assertEqual(out["context_tokens_raw"], 297329)   # 原值仍可见
        self.assertIs(out["busy"], False)                     # 非 ctx 字段照旧


if __name__ == "__main__":
    unittest.main()
