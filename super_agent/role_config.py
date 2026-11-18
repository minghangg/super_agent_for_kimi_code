"""Role symbols, comment prefixes and delivery prefixes, with a private overlay.

A role *name* is not written into this file. The repository is public, so a
deployment's own spelling of the supervisor role lives in
``config/roles.local.json`` (untracked) or in ``$SAFKC_SUPERVISOR_ALIASES``.
The public half carries only the symbol :data:`SUPERVISOR` and nine generic
role names; that is what keeps the privacy gate green while a deployment keeps
using its own spelling on the command line.

Resolution order (first hit wins, no merging of halves):

1. the built-in table below;
2. ``config/roles.local.json`` -- an untracked file that overrides the table;
3. ``$SAFKC_SUPERVISOR_ALIASES`` -- comma separated, fills what the file left
   empty.

``--role <name>`` is normalised **before** any table lookup: a built-in symbol
is used as is, an alias maps to its symbol, anything else is refused together
with the list of roles that are available. Refusing is the point -- a name that
silently falls back would put a wrong prefix on a comment, and a wrong prefix
is what makes a verdict unattributable.

:data:`DEFAULT_SUPERVISOR_PREFIX` is ``supervisor`` for a reason: the engine's
``markers.PM_PREFIX_RE`` accepts ``[PM]`` and ``[supervisor]`` only. A
configured prefix outside that set would leave every supervisor verdict line
unreadable while looking like a cosmetic setting, so
:meth:`RoleConfig.warnings` reports it instead of applying it silently.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SUPERVISOR = "supervisor"

# Comment prefixes of the generic roles. ``worker`` is here because it is a
# symbol; its prefix is never used bare -- ``[WK:<name>]`` comes from
# :data:`DEFAULT_DELIVERY`.
DEFAULT_PREFIX = {
    "pm": "PM",
    "qa": "QA",
    "worker": "WK",
    "scout": "SC",
    "steward": "ST",
    "auditor": "AU",
    "reviewer": "RV",
    "analyst": "AT",
    "planner": "PL",
}

# Roles whose comments carry a name: ``[WK:<name>]`` and friends. A bare
# ``[WK]`` is not a delivery marker at all, so a role listed here cannot emit
# its prefix without a name.
DEFAULT_DELIVERY = {
    "worker": "WK",
    "scout": "SC",
    "steward": "ST",
    "auditor": "AU",
    "reviewer": "RV",
    "analyst": "AT",
    "planner": "PL",
}

DEFAULT_SUPERVISOR_PREFIX = "supervisor"
DEFAULT_USER_AGENT = "safkc-issue-cli/0.1"

# The prefixes ``markers.PM_PREFIX_RE`` accepts. Anything else cannot be read
# back as a verdict line.
ACCEPT_PREFIXES = ("PM", "supervisor")

BUILTIN_ROLES = (SUPERVISOR,) + tuple(DEFAULT_PREFIX)

# Session keys that must never be archived when an issue is closed: they are
# registered, long-lived sessions, not one-shot workers. Names, not values --
# ``sessions.json`` also lists dozens of one-shot keys.
DEFAULT_FIXED_KEYS = ("steward", "qa", "scout", "auditor", "planner",
                      "reviewer", SUPERVISOR)
DEFAULT_FIXED_PREFIXES = ("pm-", "at-")

# Key-name prefixes that make a ``--worker-name`` look complete. Used only for
# a warning: a name that matches none of them is probably missing its prefix.
SESSION_PREFIX_HINTS = ("wk-", "pm-", "qa-", "scout-", "steward-", "auditor-",
                        "reviewer-", "at-", "analyst-", "planner-")

ENV_ALIASES = "SAFKC_SUPERVISOR_ALIASES"
DEFAULT_FILE = Path("config") / "roles.local.json"


def _split_aliases(raw) -> list[str]:
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = str(raw or "").split(",")
    out: list[str] = []
    for item in items:
        name = item.strip().lower()
        if name and name not in out:
            out.append(name)
    return out


class RoleConfig:
    """The effective role table: symbols, prefixes, aliases, fixed sessions."""

    def __init__(self, *, prefixes=None, delivery=None, aliases=None,
                 user_agent: str = DEFAULT_USER_AGENT,
                 fixed_keys=None, fixed_prefixes=None, source: str = "builtin",
                 load_error: str = ""):
        self.prefixes = dict(DEFAULT_PREFIX if prefixes is None else prefixes)
        self.prefixes[SUPERVISOR] = self.prefixes.get(
            SUPERVISOR, DEFAULT_SUPERVISOR_PREFIX)
        self.delivery = dict(DEFAULT_DELIVERY if delivery is None else delivery)
        self.delivery[SUPERVISOR] = self.delivery.get(SUPERVISOR, None)
        self.aliases = dict(aliases or {})
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.fixed_keys = tuple(fixed_keys or DEFAULT_FIXED_KEYS)
        self.fixed_prefixes = tuple(fixed_prefixes or DEFAULT_FIXED_PREFIXES)
        self.source = source
        self.load_error = load_error

    # ------------------------------------------------------------ normalise
    def canonical(self, name: str) -> str | None:
        """The symbol for ``name``, or ``None`` when it is not available."""
        text = str(name or "").strip().lower()
        if not text:
            return None
        if text in self.prefixes:
            return text
        return self.aliases.get(text)

    def prefix(self, role: str) -> str:
        """The display prefix of a canonical role symbol (no brackets)."""
        return self.prefixes.get(str(role or "").strip().lower(),
                                 DEFAULT_SUPERVISOR_PREFIX)

    def brackets(self, role: str) -> str:
        return f"[{self.prefix(role)}]"

    def delivery_prefix(self, role: str) -> str | None:
        return self.delivery.get(str(role or "").strip().lower())

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self.prefixes))

    def available(self) -> list[str]:
        """Every name ``--role`` accepts, aliases last, sorted."""
        return sorted(set(self.prefixes) | set(self.aliases))

    def session_prefixes(self) -> tuple[str, ...]:
        """Key-name prefixes that count as "carries a role prefix"."""
        extra = tuple(a + "-" if not a.endswith("-") else a
                      for a in self.aliases)
        supervisor = self.prefix(SUPERVISOR).lower()
        return tuple(dict.fromkeys(SESSION_PREFIX_HINTS + (supervisor,) + extra))

    def fixed_session_key(self, name: str) -> bool:
        text = str(name or "").strip()
        if not text:
            return False
        return text in self.fixed_keys or text.startswith(self.fixed_prefixes)

    # -------------------------------------------------------------- warnings
    def warnings(self) -> list[str]:
        """The one configuration mistake that changes how comments are read."""
        out: list[str] = []
        if self.load_error:
            out.append(f"角色配置未生效：{self.load_error}（用内置默认）")
        prefix = self.prefix(SUPERVISOR)
        if prefix.lower() not in {p.lower() for p in ACCEPT_PREFIXES}:
            out.append(
                f"supervisor 前缀 {prefix!r} 不在引擎接受集 {ACCEPT_PREFIXES} 内："
                f"该角色的裁决行会被读成 unreadable。核对：python -c \"import sys;"
                f"sys.path.insert(0,'.');from super_agent import markers as m;"
                f"print(m.parse_pm_verdict('[{prefix}] 可关单 #1 "
                f"<!-- verdict: ready -->'))\"")
        return out


def load(path: str | Path | None = None, env: str | None = None) -> RoleConfig:
    """Build the effective table: built-in, then the private file, then env."""
    prefixes = dict(DEFAULT_PREFIX)
    delivery = dict(DEFAULT_DELIVERY)
    aliases: dict[str, str] = {}
    user_agent = DEFAULT_USER_AGENT
    fixed_keys = list(DEFAULT_FIXED_KEYS)
    fixed_prefixes = list(DEFAULT_FIXED_PREFIXES)
    source = "builtin"
    error = ""

    target = Path(path) if path else DEFAULT_FILE
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            error = f"{target}: {type(exc).__name__}: {exc}"
            data = {}
        roles = (data.get("roles") if isinstance(data, dict) else None) or {}
        if isinstance(roles, dict) and roles:
            source = str(target)
            for raw_symbol, spec in roles.items():
                symbol = str(raw_symbol or "").strip().lower()
                if symbol not in BUILTIN_ROLES or not isinstance(spec, dict):
                    continue
                for alias in _split_aliases(spec.get("aliases")):
                    if alias not in BUILTIN_ROLES:
                        aliases[alias] = symbol
                prefix = str(spec.get("prefix") or "").strip()
                if prefix:
                    prefixes[symbol] = prefix
                if "delivery_prefix" in spec:
                    value = spec.get("delivery_prefix")
                    delivery[symbol] = str(value).strip() if value else None
        if isinstance(data, dict):
            agent = str(data.get("user_agent") or "").strip()
            if agent:
                user_agent = agent
            keys = data.get("fixed_session_keys")
            if isinstance(keys, list) and keys:
                fixed_keys = [str(x).strip() for x in keys if str(x).strip()]
            affixes = data.get("fixed_session_prefixes")
            if isinstance(affixes, list) and affixes:
                fixed_prefixes = [str(x).strip() for x in affixes if str(x).strip()]

    raw_env = env if env is not None else os.environ.get(ENV_ALIASES, "")
    for alias in _split_aliases(raw_env):
        if alias in BUILTIN_ROLES:
            continue
        aliases.setdefault(alias, SUPERVISOR)
        source = source if source != "builtin" else f"env:{ENV_ALIASES}"

    return RoleConfig(prefixes=prefixes, delivery=delivery, aliases=aliases,
                      user_agent=user_agent, fixed_keys=fixed_keys,
                      fixed_prefixes=fixed_prefixes, source=source,
                      load_error=error)
