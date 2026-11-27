"""The default configuration table: every key the engine reads has a home.

Split out of ``config.py`` under the QA charter's rule for over-limit files
(``rules/charters/qa.md`` §3.6): a touched module over 300 lines must end at or
below 95% of its pre-change size. The table is data, not logic, so the seam is
real - ``config.load`` merges over it and nothing else in that module needs to
sit beside it.
"""
from __future__ import annotations


DEFAULTS: dict = {
    "role": "supervisor",
    "mode": "observe",
    "state_dir": "data",
    # The pre-migration runtime root. Declared or absent (#140): there is no
    # "sibling ``state/``" default any more, because finding a directory by its
    # name answered "no legacy data" as soon as a deployment moved its root to
    # ``data``. See ``cli._legacy_root``.
    "legacy_state_dir": "",
    # ISS-101: path-like keys that are not the state root. A section rather
    # than loose top-level keys because ``agent_config.overrides()`` maps
    # ``paths.*`` as one level, and ``validate`` does not reject unknown keys --
    # an undeclared key would look configured while nothing reads it.
    # ``project_env_dir`` holds one ``<key>.md`` per project (key =
    # ``gh_projects.json`` key); nothing parses the markdown.
    "paths": {
        "project_env_dir": "",
    },
    "bus": {
        "host": "127.0.0.1",
        "port": 58800,
        "timeout_s": 2.0,
    },
    "github": {
        "source": "none",          # none | fixture | live
        "repo": "",                # owner/name, only for source=live
        "fixture": "",             # path to a JSON fixture, for source=fixture
        # Project short name -> owner/repo, from the private agent.json (#98).
        # An inline table wins over ``projects_file``; both empty means the
        # project face is unconfigured and refuses with rc=3 rather than
        # running against a built-in table that names real repositories.
        "projects": {},
        "projects_file": "",       # path to the untracked project table
        "poll_interval_s": 60,
        # The listing is one page of 100 (the API maximum) and newest-first, so
        # a cap below 100 can only ever hide old in-flight issues. The real
        # backpressure is ``max_batches``/``max_issue_rows``, which bound how
        # much work one round *consumes*.
        "max_issues": 100,
        "max_comments_per_issue": 50,
        # Labels that mean "suspend this issue": the issue stays open but the
        # engine does not look at it. Matched as a set against the labels the
        # list endpoint already returns, so no extra request. Empty list means
        # no filter (fail open) - missing an issue silently hides its frames,
        # which is worse than looking at one too many.
        "on_hold_labels": ["stage:on-hold"],
        # #134 step 2: HTTP client policy; see super_agent.github.client.
        "etag_cache": True, "local_cache_ttl_s": 300, "max_concurrent": 3,
    },
    "store": {
        "max_event_lines": 10_000,
        "max_outbox_lines": 5_000,
        "outbox_stale_s": 300,     # in_flight/uncertain older than this is stuck
    },
    "engine": {
        # Event-type allowlist: which event types the fast path reads.
        # The project/issue/action allowlist is a different dimension and lives
        # in config/allowlist.json (see super_agent.allowlist).
        "allowlist": ["issue_comment.created"],
        "at_question_port": 58731,
        "cooldown_s": 900,
        "assignments": {},         # reserved; M0 must leave this empty
        "session_events_path": "",  # workbench lifecycle stream (read-only); "" = resolve
        # ISS-79: the **session root** as one declared directory. The whole
        # session state lives in one root (``events.jsonl``, ``sessions.json``,
        # ``issue_sessions.json``, ``ledger.jsonl``) and one writer owns it, so
        # the paths cannot be derived from each other. Declaring the root is
        # level 3 of the resolution chain: explicit, then env, then this.
        # "" = fall back to the parent of the resolved stream.
        "session_state_dir": "",
        # ISS-79: the workbench checkout root, for documents (charter, its
        # playbooks). Independent of where the session stream lives, and
        # **declared only** (#140): "" = no workbench root, never inferred from
        # a directory name.
        "lwa_root": "",
        # ISS-79: the session console (``sess.py``). Declared here so the path
        # stops being assembled from a directory name; "" = the assembled
        # default. Consumed by the CLI console lookup.
        "session_console_path": "",
        "project_allowlist": "",   # "" = not configured; the CLI resolves the repo default
        "creation_mode": "pm_created",  # only pm_created is accepted
        # Ask the session service whether a candidate sid is archived. The
        # lifecycle stream has no archive row for an API-side archive, so this
        # is what stops a closed session from being treated as a live binding.
        # False = stream-only (no server call; used by tests that stay offline).
        "archived_crosscheck": True,
        # Model profile table (ISS-32). "" = not configured: choose_profile then
        # falls back to the built-in default pair and no file is read.
        "model_profiles_path": "",
        # A-1 (ISS-57): the contract file a new session reads first.
        # "" = fall back to the inert ping.
        "contract_path": "",
        # The charter root and the prefix -> charter-file tables (#98). The
        # public defaults are empty: a private deployment's charter names are
        # not published here. ``dispatch_cli`` reads the two tables through
        # ``agent_config``; ``charter_root`` is what the console lookup uses.
        "charter_root": "",
        # Where the dispatch console lives (``-m super_agent.dispatch_cli`` by
        # default). Read by ``dispatch_adapter.build``; declaring it here keeps
        # it inside ``validate`` instead of sneaking in as an unknown key.
        "dispatch": {"console": ""},
        # How many held threads one round asks the close reconciliation about.
        # ``waiting_release`` and ``close_reconcile`` are the only states that
        # need it, so the cap bounds a poll loop rather than all assignments.
        "max_closed_thread_checks": 10,
        # ISS-100 D5: how ``close`` archives the sessions it bound. One template
        # for the whole deployment, so the public default names this package and
        # nothing else: ``{python}`` is ``sys.executable`` (the interpreter that
        # runs the engine) and ``{sid}`` the session id. Both placeholders are
        # replaced **after** the command is split, so the template itself must
        # use forward slashes only -- ``shlex.split`` eats ``D:\x`` as an escape
        # and argv[0] becomes ``D:x``, a path that fails as FileNotFoundError.
        # ``""`` disables archiving (the registration is then kept, see
        # ``issue_cli._archive_session``).
        "session_archive_cmd": "{python} -m super_agent.cli recycle archive {sid}",
        # Where a shippable verdict goes (ISS-53). ``supervisor_sid`` wins when
        # set; otherwise ``supervisor_keys`` are looked up in ``sessions.json``
        # in order. The default list carries only the public role name: a
        # private key belongs in the untracked overlay, never here.
        "supervisor_sid": "",
        "supervisor_keys": ["supervisor"],
        # A thread held by close reconciliation for longer than this reminds
        # the supervisor once (at most once per start time).
        "release_timeout_s": 1800,
        # Worker/QA liveness watch (ISS-69). A bound worker or QA session that
        # is not busy for this many consecutive probes, with no new comment or
        # verdict on its thread in between, gets one exception frame to the
        # supervisor. Off by default: nobody configured it means nobody runs it.
        # busy only answers "alive or dead", never "finished" - completion stays
        # with the verdict contract.
        "worker_liveness_enabled": False,
        "worker_liveness_rounds": 2,
        "worker_liveness_interval_s": 60,
        # Accept an untrustworthy cursor and re-scan every comment (ISS-87).
        # Off by default: once a verdict has to name its session key, a re-scan
        # turns every older verdict into an ``unverified`` row. The CLI flag
        # ``--allow-empty-cursor`` sets this for one run; a config file that
        # sets it makes the re-scan permanent, which is why it is spelled out.
        "allow_empty_cursor": False,
    },
}
