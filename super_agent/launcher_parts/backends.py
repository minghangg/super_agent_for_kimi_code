"""The two read-only backends the launcher exposes (ISS-132 F).

A backend answers "what is it" - never "make it so". That asymmetry is the whole
point of the module, and each rule below exists because the opposite is easy to
write and expensive to undo:

**``get`` never restores.** An archived session is a fact, not a task. Calling
the console's revive path from a read would silently resurrect a one-shot worker,
reopen a closed binding and spend quota, all while answering a page refresh. So
:class:`KimiSessionBackend` reads the two maps and nothing else: it has no
network client, and the restore capability is *declared unsupported* rather than
left unimplemented, because ``getattr(backend, "restore", None)`` returning
``None`` reads as "not wired yet" and someone will wire it.

**A provider never becomes part of the key.** ``owner/repo#N`` is the identity of
an issue everywhere - index, cursor, ledger, comments. If a second provider
existed, keying by it would split the same issue into two threads the moment the
provider was switched, and every cursor would silently start over. The key is
therefore computed by :func:`super_agent.issuekey.thread_key` and the provider is
carried *beside* it, never inside it.

**Unsupported is an explicit failure.** A capability the backend does not have
raises :class:`UnsupportedCapability`; it never returns an empty result, because
``[]`` and ``None`` both read as "nothing there" and hide the gap.

Only Kimi and GitHub are implemented. The others named in the ticket (Obsidian,
TAPD, Pi, Claude, DSH) are deliberately absent - a stub that answers "not
implemented yet" in the same shape as a real answer is the伪成功 this test list
forbids.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .. import issuekey


class UnsupportedCapability(RuntimeError):
    """The backend was asked for something it does not have.

    Distinct from ``NotImplementedError`` on purpose: this is a **contract**
    answer (the capability will never exist on this backend), and a caller may
    catch it to fall back. ``NotImplementedError`` reads as a bug in progress.
    """

    def __init__(self, backend: str, capability: str) -> None:
        super().__init__(f"{backend}: 不支持能力 {capability!r}")
        self.backend = str(backend)
        self.capability = str(capability)


# --------------------------------------------------------------------- sessions
@dataclass(frozen=True)
class SessionRef:
    """One session as read: identity plus state, never an instruction to act."""

    sid: str
    key: str = ""
    provider: str = "kimi"
    archived: bool = False
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        out = {"sid": self.sid, "key": self.key, "provider": self.provider,
               "archived": bool(self.archived)}
        out.update(self.data)
        return out


class SessionBackend:
    """Read-only session access. ``restore`` is **not** part of the contract.

    The base declares **no** capability: a subclass that implements nothing must
    not inherit "I can read" from the interface, or an unsupported read would
    return an empty list and look like "nothing there" (counter-example 5).
    """

    provider = ""
    capabilities = frozenset()

    def supports(self, capability: str) -> bool:
        return str(capability) in self.capabilities

    def require(self, capability: str) -> None:
        if not self.supports(capability):
            raise UnsupportedCapability(self.provider or type(self).__name__,
                                        capability)

    def get(self, sid: str) -> SessionRef | None:
        raise UnsupportedCapability(self.provider or type(self).__name__, "get")

    def list(self) -> list[SessionRef]:
        raise UnsupportedCapability(self.provider or type(self).__name__, "list")

    def restore(self, sid: str) -> None:
        """Refused by contract, not by omission: reading must never revive."""
        raise UnsupportedCapability(self.provider or type(self).__name__, "restore")


class KimiSessionBackend(SessionBackend):
    """Sessions from the two maps on disk. No client, so no restore is possible.

    ``sessions`` and ``issue_sessions`` are injected readers; the default ones
    are the ledger's shared-open readers, which is what keeps a read from
    blocking the broker's ``os.replace`` on Windows (#129 P0-1).

    :meth:`for_root` is the constructor the launcher uses. It exists because the
    default readers resolve their paths from **configuration**, and a launcher
    started with ``--state-dir`` must not publish somebody else's sessions: an
    isolated run that read the production map would answer a page with 200 real
    sessions - measured, not hypothetical.

    The record's ``archived`` flag is **preserved**, not filtered: a caller that
    asks for an archived session gets it, with ``archived=True``. Dropping it
    would make "the session is gone" indistinguishable from "you typed the
    wrong sid", and the page would offer a link to nothing.
    """

    provider = "kimi"
    capabilities = frozenset({"get", "list"})

    def __init__(self, *, sessions=None, issue_sessions=None) -> None:
        self._sessions = sessions
        self._bindings = issue_sessions

    @classmethod
    def for_root(cls, root) -> "KimiSessionBackend":
        """Readers bound to one state root: ``sessions.json`` beside ``engine.pid``.

        Both files are read with the shared-open reader (never a plain
        ``open``), because on Windows a plain read handle makes the broker's
        ``os.replace`` fail with ``WinError 5`` for as long as it is held.
        """
        from pathlib import Path

        base = Path(root)

        def reader(name: str):
            def read() -> dict:
                from .. import session_map_store

                path = base / name
                try:
                    data = json.loads(session_map_store.read_text_shared(path))
                except (OSError, ValueError):
                    return {}
                return data if isinstance(data, dict) else {}

            return read

        return cls(sessions=reader("sessions.json"),
                   issue_sessions=reader("issue_sessions.json"))

    def _load_sessions(self) -> dict:
        if self._sessions is not None:
            return self._sessions()
        from .. import session_ledger
        return session_ledger.load_sessions()

    def _load_bindings(self) -> dict:
        if self._bindings is not None:
            return self._bindings()
        from .. import session_ledger
        return session_ledger.load_issue_sessions()

    @staticmethod
    def _ref(sid: str, key: str, record) -> SessionRef:
        data = dict(record) if isinstance(record, dict) else {"id": record}
        return SessionRef(sid=str(sid), key=str(key), provider="kimi",
                          archived=bool(data.get("archived")), data=data)

    def get(self, sid) -> SessionRef | None:
        """One session by id. Reads only; an archived one is returned as such."""
        want = str(sid or "")
        if not want:
            return None
        for key, record in (self._load_sessions() or {}).items():
            got = (record or {}).get("id") if isinstance(record, dict) else record
            if str(got or "") == want:
                return self._ref(want, key, record)
        for thread, roles in (self._load_bindings() or {}).items():
            if not isinstance(roles, dict):
                continue
            for role, entry in roles.items():
                got = (entry or {}).get("session") if isinstance(entry, dict) else entry
                if str(got or "") != want:
                    continue
                record = entry if isinstance(entry, dict) else {}
                return self._ref(want, f"{thread}:{role}", record)
        return None

    def list(self) -> list[SessionRef]:
        """Every session in both maps, deduped by sid. Reads only."""
        out: dict[str, SessionRef] = {}
        for key, record in (self._load_sessions() or {}).items():
            got = (record or {}).get("id") if isinstance(record, dict) else record
            if not got:
                continue
            out.setdefault(str(got), self._ref(str(got), str(key), record))
        for thread, roles in (self._load_bindings() or {}).items():
            if not isinstance(roles, dict):
                continue
            for role, entry in roles.items():
                got = (entry or {}).get("session") if isinstance(entry, dict) else entry
                if not got:
                    continue
                out.setdefault(str(got), self._ref(
                    str(got), f"{thread}:{role}",
                    entry if isinstance(entry, dict) else {}))
        return list(out.values())


# ----------------------------------------------------------------------- issues
@dataclass(frozen=True)
class IssueRef:
    """An issue identity: the canonical key plus the provider that answered.

    ``provider`` is **not** part of ``key``. Adding a provider must not move a
    single cursor, and that is exactly what a keyed-with-provider scheme does on
    the first switch.
    """

    key: str
    repo: str
    issue: int
    provider: str = "github"

    def as_dict(self) -> dict:
        return {"key": self.key, "repo": self.repo, "issue": self.issue,
                "provider": self.provider}


class IssueBackend:
    """Canonical ``owner/repo#N`` keys, provider-independent.

    Same rule as :class:`SessionBackend`: the base claims nothing.
    """

    provider = ""
    capabilities = frozenset()

    def supports(self, capability: str) -> bool:
        return str(capability) in self.capabilities

    def require(self, capability: str) -> None:
        if not self.supports(capability):
            raise UnsupportedCapability(self.provider or type(self).__name__,
                                        capability)

    def ref(self, repo, issue) -> IssueRef:
        """The canonical reference. Never raises for a bad repo: the fallback
        (a bare number) is what an existing fixture or config already means."""
        key = issuekey.thread_key(repo, issue)
        number = issuekey.issue_number(key)
        return IssueRef(key=key, repo=issuekey.repo_full(repo),
                        issue=int(number if number is not None else 0),
                        provider=self.provider)

    def read(self, repo, issue):
        raise UnsupportedCapability(self.provider or type(self).__name__, "read")


class GitHubIssueBackend(IssueBackend):
    """GitHub: only the key is implemented here.

    Reading an issue over HTTP is the engine's job and stays there - this
    backend exists so a caller can name an issue without importing the engine,
    and so the key rule has one home.
    """

    provider = "github"
    capabilities = frozenset({"key"})


# ---------------------------------------------------------------------- registry
SESSION_BACKENDS = {"kimi": KimiSessionBackend}
ISSUE_BACKENDS = {"github": GitHubIssueBackend}


def session_backend(provider: str = "kimi", **kwargs) -> SessionBackend:
    """Build a session backend by name; an unknown name is an explicit failure."""
    factory = SESSION_BACKENDS.get(str(provider or "kimi").strip().lower())
    if factory is None:
        raise UnsupportedCapability(str(provider), "session_backend")
    return factory(**kwargs)


def issue_backend(provider: str = "github") -> IssueBackend:
    factory = ISSUE_BACKENDS.get(str(provider or "github").strip().lower())
    if factory is None:
        raise UnsupportedCapability(str(provider), "issue_backend")
    return factory()
