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
