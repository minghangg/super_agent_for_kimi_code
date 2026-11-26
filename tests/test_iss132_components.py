"""L1 tests for #132: the launcher's remaining components and both backends.

Seven counter-examples, one class each, and every one is written to **fail
first** - the docstring of a class names the failure it measures, so "green"
means "the guard exists", not "the code happens to run":

1. a blocking C2 leg does not stop other components answering (measured overlap,
   not a keyword scan);
2. a QA verdict is delivered once and to **its own** project's PM (twice, or to
   another project's PM, is a failure);
3. ``get`` on an archived session returns it and never restores it;
4. adding a provider does not change the canonical key;
5. an unsupported capability raises, instead of returning a success-shaped empty;
6. ``/healthz`` is asserted on a **real HTTP response** from a real server - not
   by reading a heartbeat file;
7. the module's imports contain no third-party package, so "the test environment
   happened to have aiohttp" cannot be the reason it passed.

Run: ``python -m pytest tests/test_iss132_components.py -q``
"""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from super_agent import runner
from super_agent.launcher_parts import backends, readapi, seams
from super_agent.store import Store


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iss132_")
        self.root = Path(self.tmp.name)
        self.state = self.root / "data"
        self.state.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.state)
        self.addCleanup(self.tmp.cleanup)


# ---------------------------------------------------------------- counter-example 1
class TestBlockingLegDoesNotStopOthers(unittest.IsolatedAsyncioTestCase):
    """Counter-example 1: a 5 s block in C2 must not stall the other legs.

    The block is **threading.Event(5)**, the depth the ticket names, and the
    assertion is that the AT service keeps answering meanwhile. If the leg were
    awaited in the loop, ``answered`` would stay empty and this goes red.
    """

    async def test_at_still_answers_while_c2_blocks(self):
        release = threading.Event()
        answers = []

        def blocking_c2():
            release.wait(5.0)

        async def at_probe(stop_event):
            started = time.monotonic()
            while not stop_event.is_set():
                answers.append(time.monotonic() - started)
                await asyncio.sleep(0.01)

        rn = runner.Runner()
        rn.register("decide_watch", blocking_c2, kind=runner.THREAD, interval=3600)
        rn.register("at", at_probe, kind=runner.SERVICE)
        await rn.start()
        try:
            await asyncio.sleep(0.3)
            self.assertTrue(answers, "a blocking C2 stopped the loop")
            self.assertLess(min(answers), 0.25)
            self.assertTrue(rn.component("decide_watch").health.running)
        finally:
            release.set()
            await asyncio.wait_for(rn.stop(), timeout=5)


# ---------------------------------------------------------------- counter-example 2
class TestNoticeDeliveredOnceAndToOwnProject(Base):
    """Counter-example 2: one verdict, one send, one project's PM.

    Two failure modes, one mechanism. "Twice" is measured through
    ``enqueue_delivery``: the same evidence form must be refused the second time.
    "Next door" is measured through ``resolve_recipients``: each project resolves
    **its own** PM key, and a project with no PM leaves the delivery queued
    instead of borrowing the supervisor.

    A test that only counted sends would pass a delivery sent twice to the wrong
    project, so both halves are asserted separately.
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(self._restore)
        self.sessions = self.state / "sessions.json"
        self.sessions.write_text(json.dumps({
            "pm-alpha": {"id": "session_alpha"},
            "pm-beta": {"id": "session_beta"},
            "supervisor": {"id": "session_supervisor"},
        }), encoding="utf-8")
        self._configure()

    def _restore(self):
        from super_agent import decide_watch as dw

        dw.configure(dw.DecideWatchConfig())

    def _configure(self):
        from super_agent import decide_watch as dw

        dw.configure(dw.DecideWatchConfig(
            state_root=self.state, workspace_root=self.state,
            delivery_state_path=self.state / "delivery_watch_state.json",
            delivery_stats_path=self.state / "delivery_watch_stats.jsonl",
            delivery_log_path=self.state / "delivery_watch.log",
            undelivered_path=self.state / "delivery_undelivered.log",
            sessions_path=self.sessions,
            bind_path=self.state / "issue_sessions.json",
            ledger_path=self.state / "ledger.jsonl",
            health_state_path=self.state / "supervisor_health_state.json",
            cursor_path=self.state / "cursor.json",
            log_path=self.state / "decision_watch.log",
            lock_path=self.state / "decide_watch.lock"))

    def record(self, project: str, issue: int, evidence: str) -> dict:
        return {"v": 1, "project": project, "repo": f"owner/{project}",
                "issue": issue, "worker_name": "wk-x", "worker_session": "",
                "pm_key": f"pm-{project}", "pm_sid": "", "kind": "comment",
                "evidence": evidence, "ledger_line": 1, "detected_ts": 0.0,
                "dl_id": f"dl-{evidence}"}

    def test_same_evidence_is_delivered_once(self):
        from super_agent import decide_watch as dw

        self._configure()
        dw.DELIVERY_SINK = []
        self.addCleanup(setattr, dw, "DELIVERY_SINK", None)
        st = dw._default_state()
        first = dw.enqueue_delivery(st, self.record("alpha", 7, "cid:1"),
                                    dry=False, now=2.0)
        again = dw.enqueue_delivery(st, self.record("alpha", 7, "cid:1"),
                                    dry=False, now=2.0 + dw.DELIVERY_BURST_WINDOW_S + 1)
        self.assertEqual((first, again), (1, 0),
                         "同一条交付被投了两次")
        self.assertEqual(len(dw.DELIVERY_SINK), 1, "第二次入队仍然发出去了")

    def test_each_project_resolves_its_own_pm(self):
        from super_agent import decide_watch as dw

        alpha = dw.resolve_recipients("owner/alpha")
        beta = dw.resolve_recipients("owner/beta")
        self.assertEqual([key for key, _sid in alpha], ["pm-alpha"])
        self.assertEqual([key for key, _sid in beta], ["pm-beta"])
        self.assertNotEqual(alpha, beta, "两个项目解析到同一个收件人")

    def test_a_project_without_a_pm_borrows_nobody(self):
        """A named project with no PM answers ``[]`` - it never takes the neighbour's.

        No project at all is a different question (``preferred_key``'s documented
        "newest ``role-*``"), so it is not asserted here: conflating the two would
        pin the wrong rule.
        """
        from super_agent import decide_watch as dw

        self.assertEqual(dw.resolve_recipients("owner/gamma"), [],
                         "没有 PM 的项目借了别的收件人")


# ---------------------------------------------------------------- counter-example 3
class TestGetDoesNotRestore(Base):
    """Counter-example 3: ``get`` on an archived session returns it, never revives it.

    ``restore`` exists only to raise, so any code path that tries to revive goes
    through a documented refusal. The positive half - the archived session **is
    returned** - matters too: filtering it out would make "archived" look like
    "does not exist" and hide the row from the page.
    """

    def test_archived_session_is_returned_and_not_restored(self):
        backend = backends.KimiSessionBackend(
            sessions=lambda: {"wk-old": {"id": "session_dead", "archived": True}},
            issue_sessions=lambda: {})
        ref = backend.get("session_dead")
        self.assertIsNotNone(ref)
        self.assertTrue(ref.archived)
        with self.assertRaises(backends.UnsupportedCapability):
            backend.restore("session_dead")

    def test_session_backend_has_no_network_client(self):
        """No revive can be reached by accident: the backend imports no client."""
        source = Path(backends.__file__).read_text(encoding="utf-8")
        for token in ("import api", "import direct", "session_info("):
            self.assertNotIn(token, source, f"会话后端里出现了可恢复入口：{token}")

    def test_list_keeps_archived_rows(self):
        backend = backends.KimiSessionBackend(
            sessions=lambda: {"a": {"id": "session_a"}, "b": {"id": "session_b",
                                                            "archived": True}},
            issue_sessions=lambda: {})
        rows = {ref.sid: ref for ref in backend.list()}
        self.assertEqual(set(rows), {"session_a", "session_b"})
        self.assertTrue(rows["session_b"].archived)


# ---------------------------------------------------------------- counter-example 4
class TestProviderDoesNotChangeKey(unittest.TestCase):
    """Counter-example 4: the canonical key is ``owner/repo#N`` whatever the provider.

    A key that included the provider would move every cursor on the first switch -
    the same issue would silently start over. So the provider travels **beside**
    the key and the key is compared literally.
    """

    def test_key_is_identical_across_providers(self):
        github = backends.issue_backend("github")
        ref = github.ref("Owner/Repo", 42)
        self.assertEqual(ref.key, "owner/repo#42")
        self.assertEqual(ref.provider, "github")
        self.assertNotIn("github", ref.key)

        # A second provider with the same repo/issue must produce the same key;
        # if that ever needs a new provider class, this assertion still holds.
        class Other(backends.IssueBackend):
            provider = "other"

        self.assertEqual(Other().ref("Owner/Repo", 42).key, ref.key)

    def test_key_survives_the_dict_shape(self):
        row = backends.issue_backend().ref("owner/repo", "42").as_dict()
        self.assertEqual(row["key"], "owner/repo#42")
        self.assertEqual(json.dumps(row, sort_keys=True).count("owner/repo#42"), 1)


# ---------------------------------------------------------------- counter-example 5
class TestUnsupportedIsExplicit(unittest.TestCase):
    """Counter-example 5: an unsupported capability raises; it never looks like success.

    ``[]``/``None``/``{}`` all read as "nothing there", which is why they are the
    wrong answer for "this backend cannot do that".
    """

    def test_unknown_provider_raises(self):
        with self.assertRaises(backends.UnsupportedCapability):
            backends.session_backend("obsidian")
        with self.assertRaises(backends.UnsupportedCapability):
            backends.issue_backend("tapd")

    def test_base_capabilities_raise_rather_than_return_empty(self):
        class Bare(backends.SessionBackend):
            provider = "bare"

        backend = Bare()
        self.assertFalse(backend.supports("get"))
        with self.assertRaises(backends.UnsupportedCapability):
            backend.get("session_x")
        with self.assertRaises(backends.UnsupportedCapability):
            backend.list()
        with self.assertRaises(backends.UnsupportedCapability):
            backend.require("restore")

    def test_error_names_backend_and_capability(self):
        try:
            backends.session_backend("obsidian")
        except backends.UnsupportedCapability as exc:
            self.assertEqual(exc.backend, "obsidian")
            self.assertEqual(exc.capability, "session_backend")
        else:                                   # pragma: no cover
            self.fail("unsupported provider returned instead of raising")


# ---------------------------------------------------------------- counter-example 6 + 7
class TestHealthzIsRealHttp(Base):
    """Counter-examples 6 and 7: a **real** HTTP response, with no third-party import.

    Counter-example 6 says a heartbeat file is not ``/healthz``; so this class
    starts the server on a real port and reads the body over TCP. Counter-example
    7 says the dependency must be explicit; so the module is scanned for imports
    outside the standard library **and** the server is built in a subprocess whose
    ``sys.modules`` carries no ``aiohttp``.
    """

    def snapshot(self) -> dict:
        return {"pid": 1, "running": True, "phase": "running",
                "started_at": "2026-11-26T00:00:00Z", "stalled": [],
                "components": {
                    "engine": {"enabled": True, "running": True, "kind": "thread",
                               "started": True, "runs": 3, "overrun": 0,
                               "stalled": False, "last_error": "",
                               "last_started_at": "", "last_success_at": ""},
                    "at": {"enabled": False, "running": False, "kind": "service",
                           "started": False, "runs": 0, "overrun": 0,
                           "stalled": False, "last_error": "at_service=off",
                           "last_started_at": "", "last_success_at": ""},
                },
                # Not whitelisted on purpose: it must not appear in the response.
                "internal_secret_table": {"token": "ghp_should_never_show"}}

    def fetch(self, port: int, path: str, host: str | None = None) -> tuple[int, str]:
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if host is not None:
            request.add_header("Host", host)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return int(response.status), response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode("utf-8")

    def test_healthz_returns_real_http_json(self):
        port = free_port()
        server, thread = readapi.serve(host="127.0.0.1", port=port,
                                       snapshot=self.snapshot)
        try:
            status, body = self.fetch(port, "/healthz")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["ready"], "一条 disabled 的腿必须在 ready 上体现")
        self.assertEqual(payload["components"]["engine"]["runs"], 3)
        self.assertEqual([row["component"] for row in payload["failures"]], ["at"])
        self.assertIn("at_service=off", payload["failures"][0]["reason"])

    def test_non_whitelisted_fields_are_absent(self):
        port = free_port()
        server, thread = readapi.serve(host="127.0.0.1", port=port,
                                       snapshot=self.snapshot)
        try:
            _status, body = self.fetch(port, "/healthz")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertNotIn("internal_secret_table", body)
        self.assertNotIn("ghp_should_never_show", body)
        self.assertNotIn("internal_secret_table", body)
        self.assertEqual(set(json.loads(body)["components"]["engine"]),
                         set(readapi.COMPONENT_FIELDS))

    def test_foreign_host_is_refused(self):
        port = free_port()
        server, thread = readapi.serve(host="127.0.0.1", port=port,
                                       snapshot=self.snapshot)
        try:
            good, _ = self.fetch(port, "/healthz", host=f"127.0.0.1:{port}")
            bad, body = self.fetch(port, "/healthz", host="evil.example.com")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(good, 200)
        self.assertEqual(bad, 421, "异常 Host 必须被拒，而不是被改写")
        self.assertIn("host not allowed", body)

    def test_sessions_endpoint_is_read_only_and_declares_restore_unsupported(self):
        port = free_port()
        backend = backends.KimiSessionBackend(
            sessions=lambda: {"a": {"id": "session_a", "archived": True}},
            issue_sessions=lambda: {})
        server, thread = readapi.serve(host="127.0.0.1", port=port,
                                       snapshot=self.snapshot,
                                       sessions=backend.list)
        try:
            status, body = self.fetch(port, "/sessions")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["restore"], "unsupported")
        self.assertEqual([row["sid"] for row in payload["sessions"]], ["session_a"])
        self.assertTrue(payload["sessions"][0]["archived"])

    def test_readapi_imports_only_the_standard_library(self):
        """Scan the **import statements**, not the prose: the docstring names
        aiohttp on purpose, to say why it is not used."""
        import ast

        source = Path(readapi.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                if node.module:
                    imported.add(node.module.split(".")[0])
        third_party = imported - set(sys.stdlib_module_names)
        self.assertEqual(third_party, set(), f"readapi 引入了第三方依赖：{third_party}")

    def test_server_starts_without_aiohttp_installed(self):
        """Counter-example 7, measured: a real server in a process without aiohttp.

        ``sys.modules['aiohttp'] = None`` makes any later ``import aiohttp``
        raise ``ImportError`` in that subprocess, so the server answering proves
        the endpoint does not need it.
        """
        port = free_port()
        code = (
            "import sys\n"
            "sys.modules['aiohttp'] = None\n"
            "import json, urllib.request\n"
            "from super_agent.launcher_parts import readapi\n"
            f"s, t = readapi.serve(host='127.0.0.1', port={port}, snapshot=lambda: "
            "{'pid': 1, 'running': True, 'phase': 'running', 'stalled': [],"
            " 'components': {}})\n"
            "try:\n"
            f"    with urllib.request.urlopen('http://127.0.0.1:{port}/healthz', timeout=5) as r:\n"
            "        print(r.status, json.loads(r.read())['ok'])\n"
            "finally:\n"
            "    s.shutdown(); s.server_close(); t.join(5)\n"
        )
        proc = __import__("subprocess").run(
            [sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "200 True")


# ------------------------------------------------------------------ redaction
class TestConfigRedaction(unittest.TestCase):
    def test_values_credentials_and_pointers_are_all_removed(self):
        cfg = {"github": {"token": "ghp_realtoken", "repo": "owner/repo",
                          "token_env": "GITHUB_TOKEN"},
               "bus": {"port": 58800},
               "providers": [{"api_key": "sk-live"}, {"name": "cb"}]}
        out = readapi.redact_config(cfg)
        rendered = json.dumps(out)
        self.assertNotIn("ghp_realtoken", rendered)
        self.assertNotIn("sk-live", rendered)
        self.assertNotIn("GITHUB_TOKEN", rendered, "指针值也必须消失，不只是值")
        self.assertEqual(out["github"]["repo"], "owner/repo")
        self.assertEqual(out["bus"]["port"], 58800)

    def test_deep_nesting_is_bounded(self):
        node: dict = {"leaf": 1}
        current = node
        for _ in range(20):
            current["next"] = {}
            current = current["next"]
        json.dumps(readapi.redact_config(node))          # must not recurse forever


# --------------------------------------------------------------------- seams
class TestSeams(Base):
    def test_ps_json_reports_instead_of_raising(self):
        rc, obj, raw = seams.ps_json("throw 'boom'", timeout=20)
        self.assertNotEqual(rc, 0)
        self.assertEqual(obj, {})
        self.assertIsInstance(raw, str)

    def test_heartbeat_binds_this_state_root(self):
        beats = []
        writer = seams.heartbeat(self.state)
        from super_agent import wss_heartbeat

        original = wss_heartbeat.write_heartbeat
        wss_heartbeat.write_heartbeat = lambda name, extra=None: beats.append((name, extra))
        try:
            writer("decide_watch", {"loop": True})
        finally:
            wss_heartbeat.write_heartbeat = original
        self.assertEqual(beats, [("decide_watch", {"loop": True})])

    def test_windowless_python_always_answers(self):
        exe, note = seams.windowless_python()
        self.assertTrue(Path(exe).name.lower().startswith("python"))
        self.assertIsInstance(note, str)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------- end to end
class TestLauncherWiring(Base):
    """The host wiring, measured through ``build_runner`` - not through a mock.

    Three defects were only visible here, and each is pinned by one test below:

    * a disabled service component referenced a placeholder that lived in the
      **other** module, so turning any leg off raised ``NameError`` at build time;
    * ``failures`` counted "idle between rounds" as broken, so every healthy
      thread component was reported as failing;
    * the session backend resolved its paths from configuration, so an isolated
      ``--state-dir`` published the production map (216 rows were measured).
    """

    class Dummy:
        def run_once(self):
            return {"ok": True}

    def build(self, cfg_extra=None, **kwargs):
        from super_agent import launcher

        cfg = {"engine": {"launcher_stop_budget_s": 5},
               "github": {"poll_interval_s": 3600},
               "readapi": {"port": 0}}
        cfg.update(cfg_extra or {})
        return launcher.build_runner(cfg, self.store, engine=self.Dummy(),
                                     enable_wss=False,
                                     enable_decide_watch=False,
                                     enable_at=False,
                                     enable_reporter=False,
                                     enable_readapi=False, keep={}, **kwargs)

    def test_every_leg_off_still_builds(self):
        rn = self.build()
        self.assertEqual(rn.names(),
                         ["engine", "wss", "decide_watch", "at", "reporter", "readapi"])
        for name in ("wss", "decide_watch", "at", "reporter", "readapi"):
            self.assertFalse(rn.component(name).enabled, name)
            self.assertTrue(rn.component(name).health.last_error, name)

    def test_disabled_service_placeholder_waits_for_stop(self):
        from super_agent.launcher_parts import host

        async def run():
            stop = asyncio.Event()
            task = asyncio.create_task(host._null_service(stop))
            await asyncio.sleep(0)
            self.assertFalse(task.done(), "占位服务立即返回＝假装崩溃")
            stop.set()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(run())

    def test_idle_thread_component_is_not_a_failure(self):
        """A thread between rounds is ``running=false`` and **healthy**."""
        payload = readapi.health_payload({
            "pid": 1, "running": True, "phase": "running", "stalled": [],
            "components": {
                "engine": {"enabled": True, "running": False, "kind": "thread",
                           "started": True, "last_error": "", "stalled": False,
                           "runs": 4},
                "at": {"enabled": True, "running": True, "kind": "service",
                       "started": True, "last_error": "", "stalled": False,
                       "runs": 1},
            }})
        self.assertEqual(payload["failures"], [],
                         "空闲线程被当成故障，或已返回的服务没被报出来")
        self.assertTrue(payload["ready"])

    def test_a_returned_service_is_a_failure(self):
        payload = readapi.health_payload({
            "pid": 1, "running": True, "phase": "running", "stalled": [],
            "components": {"wss": {"enabled": True, "running": False,
                                   "kind": "service", "started": True,
                                   "last_error": "", "stalled": False, "runs": 1}}})
        self.assertEqual(payload["failures"],
                         [{"component": "wss", "reason": "service returned"}])

    def test_sessions_backend_is_bound_to_the_state_root(self):
        (self.state / "sessions.json").write_text(
            json.dumps({"wk-a": {"id": "session_a", "archived": True}}),
            encoding="utf-8")
        backend = backends.KimiSessionBackend.for_root(self.state)
        rows = backend.list()
        self.assertEqual([ref.sid for ref in rows], ["session_a"])
        self.assertTrue(rows[0].archived)

    def test_sessions_endpoint_over_http_reads_only_this_root(self):
        (self.state / "sessions.json").write_text(
            json.dumps({"wk-a": {"id": "session_a"}}), encoding="utf-8")
        port = free_port()
        backend = backends.KimiSessionBackend.for_root(self.state)
        server, thread = readapi.serve(host="127.0.0.1", port=port,
                                       snapshot=lambda: {"running": True,
                                                         "components": {}},
                                       sessions=backend.list)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/sessions",
                                        timeout=5) as response:
                payload = json.loads(response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual([row["sid"] for row in payload["sessions"]], ["session_a"])


class TestAtServiceIsBoundToThisRoot(Base):
    """The AT leg's resolvers must read **this** state root.

    Both lookups ``at_serve`` wires by default resolve paths from configuration.
    For a standalone service that is correct; for a launcher started with
    ``--state-dir`` it meant "look the AT binding up in the production map",
    measured as a 409 ("没有可用 AT 会话") for a question filed one line earlier.
    """

    def test_binding_resolver_reads_this_root(self):
        from super_agent.launcher_parts import components

        (self.state / "issue_sessions.json").write_text(json.dumps({
            "owner/repo#1": {"at": {"session": "session_here"}}}), encoding="utf-8")
        resolver = components._deployment_resolver(self.store, "binding")
        got = resolver({"thread": "owner/repo#1", "issue": 1})
        self.assertEqual(got.get("session"), "session_here")

    def test_binding_resolver_is_empty_for_unknown_thread(self):
        from super_agent.launcher_parts import components

        (self.state / "issue_sessions.json").write_text("{}", encoding="utf-8")
        resolver = components._deployment_resolver(self.store, "binding")
        self.assertEqual(resolver({"thread": "owner/repo#9"}), {})

    def test_requirement_resolver_reads_this_root(self):
        from super_agent.launcher_parts import components

        resolver = components._deployment_resolver(self.store, "requirement")
        self.assertIsNone(resolver("no-such-requirement"))

    def test_at_send_is_opt_in(self):
        """No transport unless ``engine.at_send`` says so: an answer leaving the
        machine is a decision, matching ``at_serve --allow-send``."""
        from super_agent.launcher_parts import components

        service, reason = components.build_at_service(
            {"engine": {"at_question_port": free_port()}}, self.store)
        self.assertEqual(reason, "")
        self.assertIsNone(service.transport)
        service.shutdown()

        off = dict(engine={"at_service": "off"})
        self.assertEqual(components.build_at_service(off, self.store)[0], None)


# ------------------------------------------------------- C2 leg: the four guards
class TestC2LegKeepsTheLoopGuards(Base):
    """The four guards ``decide_watch._loop`` has, each measured on a real round.

    QA cid 7183374306 returned the delivery for exactly this: the tick copied the
    consumption **order** and dropped the guards, and every dropped guard turns a
    visible fault into a silent one.

    * the supervisor probe never runs -> nobody learns the supervisor is dead;
    * no own heartbeat -> the built-in guardian reports ``missing`` forever;
    * no liveness gate -> the patrol runs once per long poll, not per interval;
    * the ``supervisor`` override never reaches the patrol -> rescue is impossible.
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(self._restore)
        self.beats = []
        self.probes = []

    def _restore(self):
        from super_agent import decide_watch as dw

        dw.configure(dw.DecideWatchConfig())

    def _configure(self, *, liveness_interval=60.0):
        from super_agent import decide_watch as dw

        class Bus:
            PORT = 58800

            def bus_get(self, path, timeout=0, port=0):
                return {"events": [], "next_seq": 1, "seq": 1,
                        "oldest_seq": 1, "boot_id": "b1"}

            def bus_health(self, port=0):
                return {"boot_id": "b1", "seq": 1}

            def bus_post(self, path, body, port=0):
                return {"ok": True}

            def guardian_thresholds(self):
                return {"decide_watch": ("heartbeat/decide.json", 900.0, "主循环"),
                        "delivery_watch": ("heartbeat/delivery.json", 900.0, "收单哨"),
                        "supervisor_notice": ("heartbeat/supervisor_notice.json",
                                              900.0, "投递哨")}

        self.bus = Bus()
        (self.state / "sessions.json").write_text(
            json.dumps({"supervisor": {"id": "session_sup"}}), encoding="utf-8")
        (self.state / "issue_sessions.json").write_text("{}", encoding="utf-8")
        (self.state / "ledger.jsonl").write_text("", encoding="utf-8")
        (self.state / "supervisor_health_state.json").write_text(
            json.dumps({"state": "ok", "ts": 0}), encoding="utf-8")
        (self.state / "decide_watch_cursor.json").write_text(
            json.dumps({"seq": 0, "boot_id": "b1"}), encoding="utf-8")
        dw.configure(dw.DecideWatchConfig(
            state_root=self.state,
            workspace_root=self.state,
            event_source=self.bus,
            heartbeat=lambda name, extra: self.beats.append(name),
            inject_event=lambda path, body, port=0: None,
            ps_json=lambda script, timeout=60.0: (0, {}, ""),
            windowless_python=lambda: (sys.executable, ""),
            probe_session=lambda sid: self.probes.append(sid) or (
                True, {"sid": "session_sup", "archived": False}),
            send_transport=lambda sid, text, timeout=60.0: None,
            liveness={"enabled": True, "rounds": 2,
                      "interval_s": float(liveness_interval)},
            liveness_state_path=self.state / "worker_liveness_state.json",
            sessions_path=self.state / "sessions.json",
            bind_path=self.state / "issue_sessions.json",
            ledger_path=self.state / "ledger.jsonl",
            cursor_path=self.state / "decide_watch_cursor.json",
            lock_path=self.state / "decide_watch.lock",
            delivery_state_path=self.state / "delivery_watch_state.json",
            delivery_stats_path=self.state / "delivery_watch_stats.jsonl",
            delivery_log_path=self.state / "delivery_watch.log",
            undelivered_path=self.state / "delivery_undelivered.log",
            health_state_path=self.state / "supervisor_health_state.json",
            log_path=self.state / "decision_watch.log",
            supervisor_key="supervisor"))

    def leg(self, **kwargs):
        from super_agent import decide_watch as dw
        from super_agent.launcher_parts.c2 import C2Leg

        # The rig passes the boundary it configured. Without that seam the leg
        # re-derives one per round and discards the rig's injections - the same
        # mechanism that made the first delivery's guards untestable.
        return C2Leg({"engine": {"decide_watch_wait_s": 0.0}}, self.store,
                     bus=self.bus, decide_watch_config=dw._cfg(),
                     wait_override=0.0, **kwargs)

    def test_guard_1_supervisor_health_is_probed_every_round(self):
        """P0-1: a dead supervisor key must be noticed by the leg, not only by ``_loop``."""
        from super_agent import decide_watch as dw

        self._configure()
        calls = []
        original = dw.check_supervisor_health
        dw.check_supervisor_health = lambda port=0: calls.append(port) or {}
        try:
            self.leg()()
        finally:
            dw.check_supervisor_health = original
        self.assertEqual(len(calls), 1, "C2 腿没有探测 supervisor 键死活")

    def test_guard_2_own_heartbeat_is_written_every_round(self):
        """P0-2: ``decide_watch`` is a built-in guardian; no beat = permanent ``missing``."""
        self._configure()
        self.leg()()
        self.assertIn("decide_watch", self.beats,
                      "C2 腿没写自身进程心跳，guardian 会持续报 missing")

    def test_guard_3_liveness_patrol_is_interval_gated(self):
        """P0-3: rounds are long polls, so counting rounds multiplies the patrol."""
        from super_agent import decide_watch as dw

        self._configure(liveness_interval=60.0)
        calls = []
        original = dw.worker_liveness_tick
        dw.worker_liveness_tick = lambda **kw: calls.append(kw) or 0
        try:
            leg = self.leg()
            for _ in range(3):
                leg()
        finally:
            dw.worker_liveness_tick = original
        self.assertEqual(len(calls), 1,
                         f"3 轮跑了 {len(calls)} 次巡检，间隔门控失效")

    def test_guard_4_supervisor_override_reaches_the_patrol(self):
        """P0-3b: ``--supervisor`` must reach the patrol, or rescue is impossible."""
        from super_agent import decide_watch as dw

        self._configure()
        calls = []
        original = dw.worker_liveness_tick
        dw.worker_liveness_tick = lambda **kw: calls.append(kw) or 0
        try:
            self.leg(supervisor="session_rescue")()
        finally:
            dw.worker_liveness_tick = original
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("supervisor"), "session_rescue",
                         "supervisor 参数没有传到活性巡检")

    def test_one_leg_instance_shares_state_across_rounds(self):
        """The gate only works if the instance survives between calls."""
        from super_agent import decide_watch as dw

        self._configure(liveness_interval=3600.0)
        calls = []
        original = dw.worker_liveness_tick
        dw.worker_liveness_tick = lambda **kw: calls.append(kw) or 0
        try:
            leg = self.leg()
            leg()
            before = leg.liveness_next
            leg()
            self.assertEqual(len(calls), 1)
            self.assertGreater(before, 0.0)
            self.assertEqual(leg.liveness_next, before, "第二轮改动了门控时间")
        finally:
            dw.worker_liveness_tick = original


class TestC2FailureHandling(Base):
    """P1-1 and P1-2: eviction rescans, and an unreachable daemon backs off."""

    def setUp(self):
        super().setUp()
        self.addCleanup(self._restore)

    def _restore(self):
        from super_agent import decide_watch as dw

        dw.configure(dw.DecideWatchConfig())

    def _configure(self, bus):
        from super_agent import decide_watch as dw

        (self.state / "supervisor_health_state.json").write_text(
            json.dumps({"state": "ok", "ts": 0}), encoding="utf-8")
        (self.state / "sessions.json").write_text("{}", encoding="utf-8")
        (self.state / "issue_sessions.json").write_text("{}", encoding="utf-8")
        (self.state / "ledger.jsonl").write_text("", encoding="utf-8")
        (self.state / "decide_watch_cursor.json").write_text(
            json.dumps({"seq": 10, "boot_id": "b1"}), encoding="utf-8")
        dw.configure(dw.DecideWatchConfig(
            state_root=self.state, workspace_root=self.state, event_source=bus,
            heartbeat=lambda name, extra: None,
            inject_event=lambda path, body, port=0: None,
            ps_json=lambda script, timeout=60.0: (0, {}, ""),
            windowless_python=lambda: (sys.executable, ""),
            probe_session=lambda sid: (True, {"sid": "s", "archived": False}),
            send_transport=lambda sid, text, timeout=60.0: None,
            liveness={"enabled": False, "rounds": 2, "interval_s": 60.0},
            sessions_path=self.state / "sessions.json",
            bind_path=self.state / "issue_sessions.json",
            ledger_path=self.state / "ledger.jsonl",
            cursor_path=self.state / "decide_watch_cursor.json",
            lock_path=self.state / "decide_watch.lock",
            delivery_state_path=self.state / "delivery_watch_state.json",
            delivery_stats_path=self.state / "delivery_watch_stats.jsonl",
            delivery_log_path=self.state / "delivery_watch.log",
            undelivered_path=self.state / "delivery_undelivered.log",
            health_state_path=self.state / "supervisor_health_state.json",
            log_path=self.state / "decision_watch.log",
            supervisor_key="supervisor"))

    def test_evicted_cursor_triggers_a_rescan_and_a_log_line(self):
        """P1-1: ``since + 1 < oldest_seq`` means rows were evicted. Say so, rescan."""
        from super_agent.launcher_parts.c2 import C2Leg

        class Bus:
            PORT = 58800

            def bus_get(self, path, timeout=0, port=0):
                return {"events": [], "next_seq": 105, "seq": 104,
                        "oldest_seq": 104, "boot_id": "b1"}

            def bus_health(self, port=0):
                return {"boot_id": "b1", "seq": 104}

            def bus_post(self, path, body, port=0):
                return {}

            def guardian_thresholds(self):
                return {"decide_watch": ("heartbeat/decide.json", 900.0, "x"),
                        "delivery_watch": ("heartbeat/delivery.json", 900.0, "y"),
                        "supervisor_notice": ("heartbeat/notice.json", 900.0, "z")}

        from super_agent import decide_watch as dw

        bus = Bus()
        self._configure(bus)
        out = C2Leg({"engine": {}}, self.store, bus=bus,
                    decide_watch_config=dw._cfg(), wait_override=0.0)()
        self.assertEqual(out.get("rescan"), "evicted", out)
        self.assertEqual(out.get("since"), 0)
        log_text = (self.state / "decision_watch.log").read_text(encoding="utf-8")
        self.assertIn("早于环形缓冲起点", log_text, "淘汰没有留下任何痕迹")

    def test_unreachable_daemon_backs_off_instead_of_raising(self):
        """P1-2: a daemon blip is the daemon's fault, not a permanent leg error."""
        from super_agent.launcher_parts.c2 import C2Leg

        class Down:
            PORT = 58800

            def bus_get(self, path, timeout=0, port=0):
                raise OSError("daemon unreachable")

            def bus_health(self, port=0):
                raise OSError("daemon unreachable")

            def bus_post(self, path, body, port=0):
                raise OSError("daemon unreachable")

            def guardian_thresholds(self):
                return {"decide_watch": ("heartbeat/decide.json", 900.0, "x"),
                        "delivery_watch": ("heartbeat/delivery.json", 900.0, "y"),
                        "supervisor_notice": ("heartbeat/notice.json", 900.0, "z")}

        from super_agent import decide_watch as dw

        down = Down()
        self._configure(down)
        leg = C2Leg({"engine": {}}, self.store, bus=down,
                    decide_watch_config=dw._cfg(), wait_override=0.0)
        first = leg()
        self.assertTrue(first.get("unreachable"), first)
        self.assertEqual(first.get("backoff_s"), 1.0)
        second = leg()
        self.assertEqual(second.get("backoff_s"), 2.0, "退避没有翻倍")


class TestIssueBackendHasAProductionCaller(Base):
    """P1-3: ``IssueBackend`` was reachable only from tests; now ``GET /issues``."""

    def test_issues_route_publishes_canonical_keys(self):
        (self.state / "index.json").write_text(json.dumps({
            "issues": {"Owner/Demo#7": {}, "other/r#2": {}}}), encoding="utf-8")
        from super_agent.launcher_parts import host

        refs, reason = host._issue_reader({"github": {"repo": "x/y"}}, self.store)
        self.assertEqual(reason, "")
        self.assertEqual([ref.key for ref in refs], ["other/r#2", "owner/demo#7"])
        self.assertTrue(all(ref.provider == "github" for ref in refs))

    def test_issues_route_reports_why_it_is_empty(self):
        from super_agent.launcher_parts import host

        refs, reason = host._issue_reader({"github": {}}, self.store)
        self.assertEqual(refs, [])
        self.assertTrue(reason, "空列表读成「没有问题单」，与别处的静默零同类")

    def test_issues_route_over_real_http(self):
        (self.state / "index.json").write_text(json.dumps({
            "issues": {"owner/demo#7": {}}}), encoding="utf-8")
        from super_agent.launcher_parts import host, readapi

        port = free_port()
        refs, _reason = host._issue_reader({"github": {}}, self.store)
        server, thread = readapi.serve(
            host="127.0.0.1", port=port,
            snapshot=lambda: {"running": True, "components": {}},
            issues=lambda: (refs, ""))
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/issues",
                                        timeout=5) as response:
                payload = json.loads(response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(payload["issues"][0]["key"], "owner/demo#7")
        self.assertEqual(payload["read"], "unsupported")


class TestReporterLegFollowsTheConfig(Base):
    """P1-4: the resident page must not silently override ``github.source``."""

    def page(self, cfg) -> str:
        from super_agent.launcher_parts import reporter_leg

        (self.state / "index.json").write_text(json.dumps({"issues": {
            "owner/demo#7": {"needs_person": [
                {"comment_id": 11, "reason": "x", "at": 1700000000.0}]}}}),
            encoding="utf-8")
        return reporter_leg.render_reporter_page(cfg, self.store).read_text(
            encoding="utf-8")

    def test_none_names_the_real_reason_not_no_quotes(self):
        text = self.page({"github": {"source": "none"}, "role": "supervisor"})
        self.assertNotIn("--no-quotes", text,
                         "常驻腿没有这个开关，却把「没联网」说成用户指定")
        self.assertIn("github.source=none", text)

    def test_a_broken_source_reports_its_own_error(self):
        text = self.page({"github": {"source": "fixture",
                                     "fixture": str(self.root / "nope.json")},
                          "role": "supervisor"})
        self.assertIn("数据源不可用", text)
        self.assertIn("nope.json", text, "真实原因被吞成了空泛说明")
        self.assertNotIn("--no-quotes", text)

    def test_live_config_really_builds_a_live_source(self):
        from super_agent import github_source

        built = github_source.build({"github": {"source": "live",
                                                "repo": "owner/demo"}})
        self.assertNotIsInstance(built, github_source.NullSource,
                                 "github.source=live 被无声覆盖成离线")

    def test_pages_differ_between_none_and_a_broken_fixture(self):
        offline = self.page({"github": {"source": "none"}, "role": "supervisor"})
        broken = self.page({"github": {"source": "fixture",
                                       "fixture": str(self.root / "nope.json")},
                            "role": "supervisor"})
        self.assertNotEqual(offline, broken,
                            "两种数据源配置产出同一页面：配置被覆盖了")
