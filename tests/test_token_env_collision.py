"""ISS-135: ``github.token_env`` must name a variable, never hold the token.

``agent_config.ENV`` mapped the **config key** ``github.token_env`` to the
**environment variable** ``GITHUB_TOKEN``. Those two are not the same kind of
thing: the key's value is a variable *name* (``"GITHUB_TOKEN"``), while the
variable's value is the token itself. ``get()`` reads the environment first, so
with ``GITHUB_TOKEN`` set the key returned ``github_pat_...``, and the whole
chain degraded from there:

1. ``token_source()`` returned ``(env_name='github_pat_...', file=<path>)``;
2. ``github.token._from_file`` looked for a line starting ``github_pat_...=``
   and found none;
3. ``_read`` then looked up ``os.environ['github_pat_...']`` and found nothing;
4. the token was ``""``, every request went anonymous, and GitHub answers an
   anonymous read of a private repository with **404** -- so the engine paused
   the thread and never processed another comment on it (11.5h, 2026-11-26).

The three tests below are the three links of that chain, in order. The first is
the direct judge: it is red before the fix and green after it. All three set
``GITHUB_TOKEN`` themselves, because the defect only exists in a process that
has it set -- the two previous investigations missed it for exactly that reason.

Run: ``python -m pytest tests/test_token_env_collision.py -q``
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from super_agent import agent_config
from super_agent.github import token as gh_token

#: What ``start_engine.ps1`` injects. A token-shaped value, not a name: the
#: assertion is that it never comes back out of a key that declares a name.
FAKE = "github_pat_FAKEVALUE123"


class _WithTokenEnv(unittest.TestCase):
    """Isolate the config file and pin ``GITHUB_TOKEN`` for the whole test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved = {k: os.environ.get(k)
                       for k in (agent_config.ENV_FILE, "GITHUB_TOKEN")}
        os.environ["GITHUB_TOKEN"] = FAKE
        os.environ[agent_config.ENV_FILE] = str(self.root / "absent.json")
        agent_config._reset_cache()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        agent_config._reset_cache()
        self._tmp.cleanup()

    def configure(self, doc: dict) -> None:
        path = self.root / "agent.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        os.environ[agent_config.ENV_FILE] = str(path)
        agent_config._reset_cache()


class TokenEnvKeyHoldsAName(_WithTokenEnv):
    """Link 1 -- the key itself. This is the red-before/green-after judge."""

    def test_the_config_key_is_not_overwritten_by_the_variable_of_the_same_name(self):
        self.assertEqual(agent_config.get("github.token_env"), "GITHUB_TOKEN")

    def test_the_declared_default_is_what_an_unconfigured_layer_reads(self):
        """The default stays in ``DEFAULTS``; only the ``ENV`` mapping goes."""
        self.assertEqual(agent_config.DEFAULTS["github.token_env"], "GITHUB_TOKEN")


class TokenSourceReturnsAName(_WithTokenEnv):
    """Link 2 -- ``token_source()`` hands the token module a variable name."""

    def test_the_first_element_is_a_variable_name_not_a_token(self):
        env_name, _file = agent_config.token_source()
        self.assertEqual(env_name, "GITHUB_TOKEN")
        self.assertNotIn(FAKE, env_name)


class TokenChainIsNotPoisoned(_WithTokenEnv):
    """Link 3 -- a valid configured token still resolves while ``GITHUB_TOKEN`` is set."""

    def setUp(self):
        super().setUp()
        env_file = self.root / "github.env"
        env_file.write_text(f"GITHUB_TOKEN={FAKE}\n", encoding="utf-8")
        self.configure({"github": {"token_env": "GITHUB_TOKEN",
                                   "token_file": str(env_file)}})

    def test_the_file_named_by_the_config_still_yields_a_token(self):
        self.assertEqual(gh_token.read_token_or_empty(), FAKE)

    def test_the_origin_is_the_file_not_an_unreadable_variable(self):
        self.assertEqual(gh_token.token_origin(), str(self.root / "github.env"))


if __name__ == "__main__":
    unittest.main()
