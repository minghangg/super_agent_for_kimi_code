"""The single HTTP exit: five counter-examples from issue #134 step 2.

Each test is the naive version's failure, not a happy path:

1. **304 body.** A 304 carries no body. A client that returns ``resp`` hands an
   empty document to a caller that asked for a listing.
2. **304 headers.** A 304 has no ``Link`` either, and ``Link`` is the only
   statement of how many comment pages exist. Dropping the cached headers makes
   the paging walk stop early -- silently.
3. **loop identity.** ``asyncio.run`` per request looks fine once and destroys
   the pool on the next call. The counter is ``new_event_loop``.
4. **``trust_env``.** ``ClientSession`` defaults it to **False**, so a client
   that omits it works on one laptop and hangs behind a corporate proxy. The
   check asks a dead proxy whether the environment was read at all.
5. **the cap.** ``Semaphore(3)`` bounds callers, ``TCPConnector(limit=3)``
   bounds sockets. Both are measured at the server: peak in flight <= 3.

Everything runs against a real socket on ``127.0.0.1`` (no public network), and
the server is deliberately spare: a real 304 has no ``Link``, so the cached
headers are the only way the second response can still state the page count.

Run: ``python -m pytest tests/test_github_client.py -q``
"""
from __future__ import annotations

import asyncio
import os
import socket
import threading
import unittest
from unittest import mock

from aiohttp import web

from super_agent import config
from super_agent.github import client as gh_client
from super_agent.github import source as source_mod
from super_agent.github.source import LiveSource, SourceError

LINK = ('<https://api.github.com/repos/owner/demo/issues/1/comments'
        '?per_page=3&page=4>; rel="last"')
ETAG = '"v1"'


class LocalApi:
    """The measured API shape over a real socket, on its own thread and loop.

    ``peak`` is counted here, at the server, because that is where "how many
    requests were in flight" is a fact rather than an intention.
    """

    def __init__(self, *, delay: float = 0.0, always_304: bool = False):
        self.delay, self.always_304 = delay, always_304
        self.seen: list[dict] = []
        self.peak = self._in_flight = 0
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever,
                                        daemon=True)
        self._runner, self.port = None, 0

    async def _handle(self, request):
        self.seen.append(dict(request.headers))
        self._in_flight += 1
        self.peak = max(self.peak, self._in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.always_304 or request.headers.get("If-None-Match") == ETAG:
                # A real 304: no body and, importantly, no ``Link``.
                return web.Response(status=304, headers={"ETag": ETAG})
            return web.json_response([{"id": 1, "body": "b1"}],
                                     headers={"ETag": ETAG, "Link": LINK})
        finally:
            self._in_flight -= 1

    def __enter__(self) -> LocalApi:
        self._thread.start()

        async def _start():
            app = web.Application()
            app.router.add_get("/{tail:.*}", self._handle)
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, "127.0.0.1", 0)
            await site.start()
            self.port = self._runner.addresses[0][1]

        asyncio.run_coroutine_threadsafe(_start(), self._loop).result(10)
        return self

    def __exit__(self, *exc_info) -> None:
        asyncio.run_coroutine_threadsafe(self._runner.cleanup(),
                                        self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)

    def url(self, path: str = "/repos/owner/demo/issues/1/comments") -> str:
        return f"http://127.0.0.1:{self.port}{path}"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        gh_client.reset()
        gh_client.configure({"etag_cache": True, "local_cache_ttl_s": 300,
                             "max_concurrent": 3})

    def tearDown(self) -> None:
        gh_client.reset()


class A304ReturnsTheCachedBody(Base):
    """Counter-example 1: the 304's body is empty; the cached one is not."""

    def test_the_second_call_returns_the_first_body(self):
        with LocalApi() as api:
            first = gh_client.get(api.url())
            second = gh_client.get(api.url())
        self.assertEqual(first.status, 200)
        self.assertFalse(first.from_cache)
        self.assertEqual(second.status, 304)
        self.assertTrue(second.from_cache)
        self.assertEqual(second.body, first.body)
        self.assertEqual(second.body, [{"id": 1, "body": "b1"}])

    def test_the_revalidation_actually_sent_the_etag(self):
        with LocalApi() as api:
            gh_client.get(api.url())
            gh_client.get(api.url())
            seen = list(api.seen)
        self.assertNotIn("If-None-Match", seen[0])
        self.assertEqual(seen[1].get("If-None-Match"), ETAG,
                         "a 304 that was never asked for is a fabricated cache")


class A304KeepsTheCachedHeaders(Base):
    """Counter-example 2: a 304 has no ``Link``; the cached one must survive."""

    def test_link_survives_the_revalidation(self):
        with LocalApi() as api:
            first = gh_client.get(api.url())
            second = gh_client.get(api.url())
        self.assertEqual(first.headers.get("Link"), LINK)
        self.assertEqual(second.headers.get("Link"), LINK,
                         "a 304 without the cached Link ends the walk early")
        self.assertEqual(LiveSource._link_last_page(second.headers), 4)

    def test_an_uncached_304_is_an_error_not_an_empty_answer(self):
        """No stored copy: returning ``[]`` would look like "no comments".

        GitHub can answer 304 to a request the client never made (a proxy
        revalidation, a stale ``If-None-Match`` from a caller). Falling back to
        an empty body there is what makes a whole listing disappear.
        """
        with LocalApi(always_304=True) as api, \
                self.assertRaises(gh_client.HttpError) as caught:
            gh_client.get(api.url())
        self.assertEqual(caught.exception.status, 304)


class OneResidentLoop(Base):
    """Counter-example 3: ``asyncio.run`` per request creates a loop per call."""

    def test_the_loop_object_is_the_same_before_and_after(self):
        before = gh_client.loop()
        self.assertTrue(before.is_running())
        with LocalApi() as api:
            for _ in range(3):
                gh_client.get(api.url())
        self.assertIs(gh_client.loop(), before)
        self.assertFalse(before.is_closed())

    def test_no_new_loop_is_created_after_the_first_request(self):
        with LocalApi() as api:
            gh_client.get(api.url())
            with mock.patch("asyncio.new_event_loop",
                            side_effect=AssertionError("loop churn")) as made:
                for _ in range(3):
                    gh_client.get(api.url())
        self.assertEqual(made.call_count, 0)
        self.assertIsNot(gh_client._THREAD, threading.current_thread())


class TheSessionTrustsTheEnvironment(Base):
    """Counter-example 4: ``ClientSession`` defaults ``trust_env`` to False."""

    def test_the_live_session_says_so(self):
        facts = gh_client.session_facts()
        self.assertTrue(facts["trust_env"])
        self.assertFalse(facts["closed"])

    def test_a_dead_proxy_in_the_environment_is_used(self):
        """If the env is ignored the request leaves directly and connects."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead = f"http://127.0.0.1:{probe.getsockname()[1]}"
        env = {"HTTP_PROXY": dead, "http_proxy": dead, "HTTPS_PROXY": dead,
               "NO_PROXY": "", "no_proxy": ""}
        with mock.patch.dict(os.environ, env), \
                self.assertRaises(gh_client.HttpError) as caught:
            gh_client.get("http://proxy-probe.invalid/x", timeout_s=5)
        self.assertIn("Proxy", caught.exception.kind,
                      f"environment was not consulted: {caught.exception}")


class TheCapHoldsAtThree(Base):
    """Counter-example 5: an unbounded session fires all callers at once."""

    def test_no_more_than_the_cap_is_in_flight(self):
        with LocalApi(delay=0.05) as api:
            results: list = []
            errors: list = []

            def one() -> None:
                try:
                    results.append(gh_client.get(api.url()))
                except Exception as exc:  # noqa: BLE001 - reported below
                    errors.append(exc)

            threads = [threading.Thread(target=one) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            peak = api.peak
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertLessEqual(peak, 3, f"in-flight peak was {peak}")
        self.assertGreater(peak, 1, "the cap serialised the calls instead")

    def test_the_configured_cap_reaches_the_connector(self):
        gh_client.reset()
        gh_client.configure({"max_concurrent": 2})
        facts = gh_client.session_facts()
        self.assertEqual(facts["connector_limit"], 2)
        self.assertEqual(facts["semaphore_limit"], 2)


class LiveSourceUsesTheClient(Base):
    """The source keeps ``(payload, headers)`` and ``SourceError`` unchanged."""

    def test_a_response_becomes_payload_and_headers(self):
        src = LiveSource("owner/demo", token="tok")
        fake = gh_client.Response([{"id": 1}], {"link": LINK}, 200)
        with mock.patch.object(source_mod.client, "get",
                               return_value=fake) as get:
            body, headers = src._get_with_headers(
                "/repos/owner/demo/issues/1/comments", {"page": 2})
        self.assertEqual(body, [{"id": 1}])
        self.assertEqual(headers, {"link": LINK})
        self.assertTrue(get.call_args.args[0].endswith("?page=2"))
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"],
                         "Bearer tok")

    def test_the_error_texts_survive_the_rewrite(self):
        """``SourceError`` wording is what operators grep; keep both shapes."""
        src = LiveSource("owner/demo")
        cases = [
            (gh_client.HttpError("Not Found", status=404, url="u"),
             "GitHub 404 on /repos/owner/demo/issues/9: Not Found"),
            (gh_client.HttpError("boom", kind="ClientConnectorError", url="u"),
             "ClientConnectorError: boom"),
        ]
        for err, expected in cases:
            with self.subTest(expected=expected), \
                    mock.patch.object(source_mod.client, "get", side_effect=err):
                with self.assertRaises(SourceError) as caught:
                    src._get_with_headers("/repos/owner/demo/issues/9")
                self.assertEqual(str(caught.exception), expected)


class ConfigDeclaresThePolicy(unittest.TestCase):
    def test_the_three_defaults_are_present(self):
        gh = config.DEFAULTS["github"]
        self.assertIs(gh["etag_cache"], True)
        self.assertEqual(gh["local_cache_ttl_s"], 300)
        self.assertEqual(gh["max_concurrent"], 3)

    def test_build_adopts_the_section(self):
        source_mod.build({"github": {"source": "none", "max_concurrent": 2}})
        self.assertEqual(gh_client.policy()["max_concurrent"], 2)
        gh_client.configure(config.DEFAULTS["github"])

    def test_a_wrong_type_degrades_to_the_safe_side(self):
        """No type check in ``config.validate``; the client normalises instead.

        ``bool("false")`` is True, so a string is read as "cache on", a
        negative TTL as "no caching", and a cap of 0 as 1. Every wrong value
        lands on the side that still answers requests.
        """
        gh_client.configure({"etag_cache": "false", "local_cache_ttl_s": -5,
                             "max_concurrent": 0})
        self.assertEqual(gh_client.policy(),
                         {"etag_cache": True, "local_cache_ttl_s": 0.0,
                          "max_concurrent": 1})
        gh_client.configure(config.DEFAULTS["github"])


if __name__ == "__main__":
    unittest.main()
