#!/usr/bin/env python3
"""
team-sync: share status, decisions and file "touches" between Claude Code agents.

Storage: one JSON file per agent on a dedicated git branch (default `team-sync`):

    rooms/<room>/room.json
    rooms/<room>/agents/<agent-id>.json

Writes never check that branch out. They go through a temporary index and an
atomic compare-and-swap ref update, so any number of worktrees and sessions
on one machine can write at the same time and see each other instantly
(worktrees share refs). Machines exchange the branch through a normal git
remote. Every agent only writes its own file, so merges are trivial:
per-file last-writer-wins.

The same script is the CLI that agents call (`team.py update ...`) and the
hook handler that Claude Code calls (`team.py hook <event>`).

Stdlib only. Python 3.8+.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Set, Tuple

VERSION = "0.4.8"
SCHEMA = 1
CONFIG_FILE = ".team-sync.json"
PUSH_ERROR_FILE = "push_error.json"  # local only, in Repo.shared_dir; never on the shared branch
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
STATUSES = ("idle", "in_progress", "blocked", "done")
ROLES = ("member", "observer")
MAX_TEXT = 400
MAX_DESCRIPTION = 1000
MAX_DIGEST_LINES = 14
MAX_ACTIVE_CUSTOM_FIELDS = 30
MAX_ACTIVE_RULES = 30

# room config: allowlisted policy names (never branch/remote/auto_observe) and the
# custom-field key syntax (anything else matching this isn't a DEFAULTS name)
POLICY_ALLOWED = {
    "overlap_action", "overlap_granularity", "stale_after_min", "touch_ttl_min",
    "digest_interval_sec", "fetch_interval_sec", "breaking_check",
}
RESERVED_FIELD_NAMES = {"branch", "remote", "auto_observe"}
CUSTOM_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TEMPLATE_TOP_KEYS = {"title", "description", "goal", "rules", "policy", "fields"}

DEFAULTS: Dict[str, Any] = {
    "branch": "team-sync",        # git branch that holds the shared state
    "remote": "origin",           # null/"none" -> local only (same machine)
    "fetch_interval_sec": 60,     # min seconds between automatic fetch/push
    "heartbeat_interval_sec": 600,
    "stale_after_min": 30,        # no heartbeat for this long -> ignored
    "touch_ttl_min": 120,         # auto-recorded edits expire after this
    "overlap_granularity": "dir",  # "file" | "dir" (same directory counts)
    "overlap_action": "warn",     # "ask" | "warn" | "deny"
    "auto_observe": None,         # room name: every session here observes it
    "digest_interval_sec": 120,   # no-op, kept for backwards compatibility (v0.3: no mid-turn digests)
    "max_items": 100,             # per list (learnings, decisions, ...); whole file is rewritten per write
    "breaking_check": True,       # end-of-turn reminder to announce your own breaking changes
}

SECRET_PATTERNS = [
    re.compile(p)
    for p in (
        r"AKIA[0-9A-Z]{16}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bgh[pousr]_[A-Za-z0-9]{30,}",
        r"\bsk-[A-Za-z0-9_-]{20,}",
        r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
        r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*\S{6,}",
    )
]

CMD = 'python3 "{}"'.format(os.path.abspath(__file__))

BREAKING_IMPACT_INSTR = (
    "For each BREAKING CHANGE above: start your answer with a one-line warning (who "
    "changed what), then a quick impact check: search this repo for the affected "
    "symbol, route or field and list the places in code you work on, or say none "
    "found. Propose the adaptation; don't apply it unless your human agrees."
)
BREAKING_IMPACT_INSTR_SHORT = (
    "For each: start with a one-line warning (who changed what), then a quick "
    "impact check (grep the affected symbol/route/field); propose the adaptation, "
    "don't apply it unless your human agrees."
)
READY_INSTR = (
    "For each READY item: tell your human in one line at the start of your answer. "
    "If your current task needs it, use it as described there (no need to ask the "
    "announcing agent first); if it is on main and you don't have it yet, run "
    "`git pull --rebase --autostash` first and say so."
)
STOP_INCOMING_INSTR = (
    "Before you finish, add to your answer a one-line warning for each (who changed "
    "what), then a quick impact check: search this repo for the affected symbol, "
    "route or field and list the places in code you work on, or say none found. "
    "Propose the adaptation; don't apply it unless your human agrees."
)
POST_EDIT_BREAKING_REMINDER = (
    '[team-sync] Before your final answer: if this task changes anything other '
    'agents rely on (exported function signature or return value, API route, '
    'schema or types, config or env var, removed or renamed file), announce it '
    'with `{} change "<what changed and what callers must do>" --breaking` and '
    "mention that in your answer. If nothing is breaking, don't mention this note."
).format(CMD)


# --------------------------------------------------------------- utilities


class TeamError(Exception):
    """User-facing error."""


class NotARepo(Exception):
    pass


class GitError(Exception):
    pass


def now() -> _dt.datetime:
    fake = os.environ.get("TEAM_SYNC_NOW")  # tests only
    if fake:
        t = parse_ts(fake)
        if t:
            return t
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def iso(t: _dt.datetime) -> str:
    return t.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(now())


def parse_ts(s: Any) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except (TypeError, ValueError):
        return None


def age_sec(ts: Any) -> Optional[float]:
    t = parse_ts(ts)
    return None if t is None else (now() - t).total_seconds()


def within(ts: Any, sec: float) -> bool:
    """True if ts is at most sec old. An age of 0 counts (don't use `age_sec() or ...`)."""
    a = age_sec(ts)
    return a is not None and a < sec


def human_age(ts: Any) -> str:
    a = age_sec(ts)
    if a is None:
        return "?"
    if a < 60:
        return "just now"
    if a < 3600:
        return "{}m ago".format(int(a // 60))
    if a < 86400:
        return "{}h ago".format(int(a // 3600))
    return "{}d ago".format(int(a // 86400))


def slug(s: str, default: str = "agent") -> str:
    out = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return out[:40] or default


def read_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "{}.{}.tmp".format(path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def dumps_doc(doc: Dict[str, Any]) -> bytes:
    clean = {k: v for k, v in doc.items() if not k.startswith("_")}
    return (json.dumps(clean, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def clean_text(text: str, field: str, limit: int = MAX_TEXT) -> str:
    text = " ".join((text or "").split())
    if not text:
        raise TeamError("{}: text is empty".format(field))
    if len(text) > limit:
        raise TeamError(
            "{}: too long ({} > {} chars). Keep entries short.".format(
                field, len(text), limit
            )
        )
    for p in SECRET_PATTERNS:
        if p.search(text):
            raise TeamError(
                "{}: looks like it contains a secret, refusing to share it.".format(field)
            )
    return text


# --------------------------------------------------------------------- git


class Git:
    def __init__(self, cwd: str):
        self.cwd = cwd

    def call(self, args: List[str], input: Any = None, env: Optional[Dict[str, str]] = None,
             timeout: float = 20) -> subprocess.CompletedProcess:
        e = dict(os.environ)
        e["GIT_TERMINAL_PROMPT"] = "0"  # never hang on a credential prompt
        if env:
            e.update(env)
        if isinstance(input, str):
            input = input.encode("utf-8")
        kw: Dict[str, Any] = {}
        if input is None:
            kw["stdin"] = subprocess.DEVNULL
        else:
            kw["input"] = input
        return subprocess.run(
            ["git"] + list(args), cwd=self.cwd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=e, timeout=timeout, **kw
        )

    def run(self, args: List[str], input: Any = None, env: Optional[Dict[str, str]] = None,
            timeout: float = 20, binary: bool = False, check: bool = True) -> Any:
        p = self.call(args, input=input, env=env, timeout=timeout)
        if check and p.returncode != 0:
            raise GitError("git {} failed: {}".format(
                " ".join(args[:2]), p.stderr.decode("utf-8", "replace").strip()))
        if binary:
            return p.stdout
        return p.stdout.decode("utf-8", "replace").strip()


class Repo:
    def __init__(self, cwd: str):
        if not cwd or not os.path.isdir(cwd):
            raise NotARepo(cwd)
        try:
            top = Git(cwd).run(["rev-parse", "--show-toplevel"])
        except (GitError, OSError):
            raise NotARepo(cwd)
        if not top:
            raise NotARepo(cwd)
        self.root = os.path.realpath(top)
        self.git = Git(self.root)
        gd = self.git.run(["rev-parse", "--git-dir"])
        cd = self.git.run(["rev-parse", "--git-common-dir"])
        self.git_dir = os.path.realpath(os.path.join(self.root, gd))
        self.common_dir = os.path.realpath(os.path.join(self.root, cd))
        # per worktree: identity, session state
        self.state_dir = os.path.join(self.git_dir, "team-sync")
        # shared by all worktrees of this clone: fetch stamp, temp indexes, log
        self.shared_dir = os.path.join(self.common_dir, "team-sync")

    def config(self, key: str) -> str:
        return self.git.run(["config", "--get", key], check=False)

    def current_branch(self) -> str:
        b = self.git.run(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        return "" if b == "HEAD" else b

    def rel(self, path: str, base: Optional[str] = None) -> Optional[str]:
        """Repo-relative POSIX path, or None if the path is outside the repo."""
        if not path:
            return None
        if not os.path.isabs(path):
            path = os.path.join(base or self.root, path)
        full = os.path.realpath(path)
        if full == self.root:
            return ""
        if not full.startswith(self.root + os.sep):
            return None
        return os.path.relpath(full, self.root).replace(os.sep, "/")

    def log_error(self, msg: str) -> None:
        try:
            os.makedirs(self.shared_dir, exist_ok=True)
            path = os.path.join(self.shared_dir, "error.log")
            if os.path.exists(path) and os.path.getsize(path) > 200_000:
                os.replace(path, path + ".1")
            with open(path, "a", encoding="utf-8") as f:
                f.write("{} {}\n".format(now_iso(), msg))
        except OSError:
            pass


def load_config(repo: Repo) -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    data = read_json(os.path.join(repo.root, CONFIG_FILE))
    if isinstance(data, dict):
        cfg.update({k: v for k, v in data.items() if not k.startswith("_")})
    env_remote = os.environ.get("CLAUDE_TEAM_REMOTE")
    if env_remote is not None:
        cfg["remote"] = env_remote
    if str(cfg.get("remote") or "").lower() in ("", "none", "local", "null"):
        cfg["remote"] = None
    return cfg


# ------------------------------------------------------------------- store


class Store:
    """Reads and writes files on the team branch without checking it out."""

    def __init__(self, repo: Repo, cfg: Dict[str, Any]):
        self.repo = repo
        self.git = repo.git
        self.cfg = cfg
        self.branch = cfg["branch"]
        self.ref = "refs/heads/" + self.branch
        self.remote = cfg.get("remote")
        self.remote_ref = "refs/remotes/{}/{}".format(self.remote, self.branch)
        self.author = "team-sync"
        self._remote_ok: Optional[bool] = None

    # -- reading

    def tip(self, ref: Optional[str] = None) -> Optional[str]:
        out = self.git.run(["rev-parse", "--verify", "-q", (ref or self.ref) + "^{commit}"],
                           check=False)
        return out or None

    def read_file(self, path: str, commit: Optional[str] = None) -> Optional[bytes]:
        commit = commit or self.tip()
        if not commit:
            return None
        p = self.git.call(["cat-file", "blob", "{}:{}".format(commit, path)])
        return p.stdout if p.returncode == 0 else None

    def list_files(self, commit: Optional[str], prefix: str = "") -> Dict[str, bytes]:
        if not commit:
            return {}
        args = ["ls-tree", "-r", "-z", commit]
        if prefix:
            args += ["--", prefix]
        out = self.git.run(args, binary=True)
        entries: List[Tuple[str, str]] = []
        for rec in out.split(b"\0"):
            if not rec or b"\t" not in rec:
                continue
            meta, path = rec.split(b"\t", 1)
            parts = meta.split(b" ")
            if len(parts) == 3 and parts[1] == b"blob":
                entries.append((path.decode("utf-8"), parts[2].decode()))
        if not entries:
            return {}
        data = self.git.run(["cat-file", "--batch"],
                            input="\n".join(s for _, s in entries) + "\n", binary=True)
        result: Dict[str, bytes] = {}
        pos = 0
        for path, _sha in entries:
            nl = data.index(b"\n", pos)
            header = data[pos:nl].split(b" ")
            size = int(header[2])
            start = nl + 1
            result[path] = data[start:start + size]
            pos = start + size + 1
        return result

    # -- writing

    def _ident_env(self) -> Dict[str, str]:
        name = self.author or "team-sync"
        return {
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": "team-sync@localhost",
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": "team-sync@localhost",
        }

    def _commit(self, parents: List[str], base: Optional[str], updates: Dict[str, bytes],
                message: str) -> Optional[str]:
        """Commit base-tree + updates. Returns None if nothing would change."""
        os.makedirs(self.repo.shared_dir, exist_ok=True)
        fd, idx = tempfile.mkstemp(prefix="index-", dir=self.repo.shared_dir)
        os.close(fd)
        os.remove(idx)  # git refuses an empty index file; let it create one
        env = {"GIT_INDEX_FILE": idx}
        try:
            if base:
                self.git.run(["read-tree", base], env=env)
            else:
                self.git.run(["read-tree", "--empty"], env=env)
            lines = []
            for path, data in sorted(updates.items()):
                blob = self.git.run(["hash-object", "-w", "--stdin"], input=data)
                lines.append("100644 {}\t{}".format(blob, path))
            if lines:
                self.git.run(["update-index", "--index-info"],
                             input="\n".join(lines) + "\n", env=env)
            tree = self.git.run(["write-tree"], env=env)
            if base and tree == self.git.run(["rev-parse", base + "^{tree}"]):
                return None
            args = ["commit-tree", "--no-gpg-sign", tree, "-m", message]
            for p in parents:
                args += ["-p", p]
            return self.git.run(args, env=self._ident_env())
        finally:
            for f in (idx, idx + ".lock"):
                try:
                    os.remove(f)
                except OSError:
                    pass

    def _cas(self, new: str, old: Optional[str]) -> bool:
        p = self.git.call(["update-ref", "-m", "team-sync", self.ref, new, old or ""])
        return p.returncode == 0

    def write(self, updates: Dict[str, bytes], message: str) -> Optional[str]:
        for attempt in range(30):
            parent = self.tip()
            sha = self._commit([parent] if parent else [], parent, updates, message)
            if sha is None:
                return parent
            if self._cas(sha, parent):
                return sha
            # another worktree won the race: back off with jitter and retry
            time.sleep(random.uniform(0.01, 0.05) * min(attempt + 1, 8))
        raise TeamError("could not update the team branch (too much contention)")

    # -- syncing with the remote

    def remote_ok(self) -> bool:
        if self._remote_ok is None:
            self._remote_ok = bool(self.remote) and self.remote in self.git.run(
                ["remote"], check=False).split()
        return self._remote_ok

    def _is_ancestor(self, a: str, b: str) -> bool:
        return self.git.call(["merge-base", "--is-ancestor", a, b]).returncode == 0

    def last_sync_age(self) -> Optional[float]:
        try:
            return time.time() - os.path.getmtime(os.path.join(self.repo.shared_dir, "last_fetch"))
        except OSError:
            return None

    def _merge(self, local: str, remote: str) -> str:
        lf = self.list_files(local)
        rf = self.list_files(remote)
        updates = {}
        for path, ldata in lf.items():
            rdata = rf.get(path)
            if rdata is None or (rdata != ldata and _newer(ldata, rdata)):
                updates[path] = ldata
        sha = self._commit([remote, local], remote, updates, "team-sync: merge")
        return sha or remote

    def _push_error_path(self) -> str:
        return os.path.join(self.repo.shared_dir, PUSH_ERROR_FILE)

    def _clear_push_error(self) -> None:
        try:
            os.remove(self._push_error_path())
        except OSError:
            pass

    def _record_push_error(self, reason: str) -> None:
        try:
            write_json(self._push_error_path(),
                      {"at": now_iso(), "remote": self.remote, "reason": reason})
        except OSError:
            pass

    def _push(self) -> str:
        p = self.git.call(["push", "--quiet", "--no-verify", self.remote,
                           "{}:refs/heads/{}".format(self.ref, self.branch)], timeout=20)
        if p.returncode == 0:
            self._clear_push_error()
            return "pushed"
        err = p.stderr.decode("utf-8", "replace")
        is_race, reason = _classify_push_error(err, self.remote)
        if is_race:
            return "rejected"
        self.repo.log_error("push failed: " + err.strip()[:300])
        self._record_push_error(reason)
        return "push-failed"

    def sync(self, force: bool = False) -> str:
        """Fetch the remote branch, merge, push. Throttled unless force."""
        if not self.remote_ok():
            return "local-only"
        stamp = os.path.join(self.repo.shared_dir, "last_fetch")
        age = self.last_sync_age()
        if not force and age is not None and age < float(self.cfg["fetch_interval_sec"]):
            return "throttled"
        os.makedirs(self.repo.shared_dir, exist_ok=True)
        with open(stamp, "a"):
            pass
        os.utime(stamp, None)
        for _ in range(3):
            p = self.git.call(["fetch", "--quiet", "--no-tags", self.remote,
                               "+{}:{}".format(self.ref, self.remote_ref)], timeout=20)
            if p.returncode == 0:
                remote_tip = self.tip(self.remote_ref)
            else:
                err = p.stderr.decode("utf-8", "replace")
                if "couldn't find remote ref" in err.lower():
                    remote_tip = None
                else:
                    self.repo.log_error("fetch failed: " + err.strip()[:300])
                    return "fetch-failed"
            local = self.tip()
            if remote_tip is None:
                return self._push() if local else "empty"
            if local is None:
                if self._cas(remote_tip, None):
                    return "updated"
                continue
            if local == remote_tip:
                return "in-sync"
            if self._is_ancestor(local, remote_tip):
                if self._cas(remote_tip, local):
                    return "updated"
                continue
            if not self._is_ancestor(remote_tip, local):
                merged = self._merge(local, remote_tip)
                if not self._cas(merged, local):
                    continue
            res = self._push()
            if res != "rejected":
                return res
        return "gave-up"


_PUSH_PERMISSION_MARKERS = (
    "remote rejected", "not allowed to push", "permission denied", "403",
    "could not read from remote repository", "authentication failed",
    "could not read username", "invalid username or password",
)


def _first_stderr_line(err: str) -> str:
    """First line of push stderr worth showing a human, trimmed to ~120 chars."""
    for line in err.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("hint:") or line.lower().startswith("to "):
            continue
        line = re.sub(r"^(remote:|error:|fatal:)\s*", "", line, flags=re.I).strip()
        if line:
            return line[:120]
    return "push failed"


def _classify_push_error(err: str, remote: str) -> Tuple[bool, str]:
    """Classifies a failed `git push`'s stderr. Returns (is_race, reason).

    is_race True: an ordinary concurrent-write race (non-fast-forward, "fetch
    first", or a plain `[rejected]`) - retry, give up silently like today.
    is_race False: a failure worth recording for the human, e.g. `[remote
    rejected]` (server hook or permission) or an auth/permission error; reason
    is a short human summary."""
    low = err.lower()
    if any(m in low for m in _PUSH_PERMISSION_MARKERS):
        return False, "no write access to {}".format(remote)
    if "non-fast-forward" in low or "fetch first" in low or "[rejected]" in low:
        return True, ""
    return False, _first_stderr_line(err)


def _newer(a: bytes, b: bytes) -> bool:
    """True if JSON doc a is newer than b. Each file has a single writer (its
    agent), which bumps `rev` on every save, so rev orders versions exactly;
    timestamps only break ties (e.g. the same identity used on two machines)."""
    def key(raw: bytes) -> Tuple[int, str]:
        try:
            d = json.loads(raw.decode("utf-8"))
            return (int(d.get("rev", 0)),
                    max(str(d.get("updated", "")), str(d.get("heartbeat", ""))))
        except (ValueError, AttributeError, TypeError):
            return (-1, "")
    return key(a) > key(b)


# ---------------------------------------------------------------- identity


def default_owner(repo: Repo) -> str:
    env = os.environ.get("CLAUDE_TEAM_OWNER")
    if env:
        return slug(env, "dev")
    email = repo.config("user.email")
    if email:
        return slug(email.split("@")[0], "dev")
    name = repo.config("user.name")
    if name:
        return slug(name.split()[0], "dev")
    return slug(os.environ.get("USER", ""), "dev")


def resolve_identity(repo: Repo, cfg: Dict[str, Any]) -> Optional[Dict[str, str]]:
    env_room = os.environ.get("CLAUDE_TEAM_ROOM", "").strip()
    if env_room.lower() in ("off", "none", "0", "false"):
        return None
    saved = read_json(os.path.join(repo.state_dir, "identity.json")) or {}
    if env_room:
        room = env_room
        agent = os.environ.get("CLAUDE_TEAM_AGENT") or saved.get("agent")
        role = os.environ.get("CLAUDE_TEAM_ROLE") or "member"
        source = "env"
    elif saved.get("room"):
        room, agent, role, source = saved["room"], saved.get("agent"), saved.get("role"), "joined"
    elif cfg.get("auto_observe"):
        room, agent, role, source = str(cfg["auto_observe"]), None, "observer", "auto"
    else:
        return None
    owner = slug(os.environ.get("CLAUDE_TEAM_OWNER") or saved.get("owner") or default_owner(repo),
                 "dev")
    agent = slug(agent or os.path.basename(repo.root))
    aid = agent if (agent == owner or agent.startswith(owner + "-")) else "{}-{}".format(owner, agent)
    return {
        "room": slug(room, "room"), "agent": agent, "owner": owner, "id": aid,
        "role": role if role in ROLES else "member", "source": source,
    }


# ---------------------------------------------------------------- docs


def agent_path(room: str, aid: str) -> str:
    return "rooms/{}/agents/{}.json".format(room, aid)


def room_path(room: str) -> str:
    return "rooms/{}/room.json".format(room)


# ------------------------------------------------------------ room config
#
# One file per writer: rooms/<room>/config/<writer-id>.json. "writer" is the
# agent id when joined as a member, or the human's owner slug when running the
# CLI without joining. Within that one file, each key the writer has ever set
# gets a {"value", "clock", "at"} entry; `null` means "unset" (files are never
# deleted, like agent docs). The file's own top-level `rev` is bumped on every
# save, so the existing per-file `_newer` merge applies unchanged: conflicts
# between writers never happen because each key's winner is resolved from the
# union of every writer's file, not from any single file's `rev`.
#
# Effective value of a key = the entry with the highest (clock, at, writer).
# A write sets clock = 1 + the highest clock seen for that key across every
# writer file (a Lamport clock), so an edit made after seeing another always
# wins, regardless of wall-clock skew between machines.


def config_prefix(room: str) -> str:
    return "rooms/{}/config/".format(room)


def config_path(room: str, writer: str) -> str:
    return "rooms/{}/config/{}.json".format(room, writer)


def is_custom_key(key: str) -> bool:
    return key not in ("title", "description", "goal") and not key.startswith(
        "rule."
    ) and not key.startswith("policy.")


def classify_key(key: str) -> Tuple[str, str]:
    """Returns (kind, full_key). Raises TeamError for a key that cannot be set."""
    if key in ("title", "description", "goal"):
        return "simple", key
    if key.startswith("rule."):
        if not re.match(r"^rule\.[a-z0-9-]+\.r\d+$", key):
            raise TeamError("bad rule key: {}".format(key))
        return "rule", key
    if key.startswith("policy."):
        name = key[len("policy."):]
        if name not in POLICY_ALLOWED:
            raise TeamError(
                "'{}' is not a room policy you can set. Allowed: {}".format(
                    key, ", ".join("policy." + n for n in sorted(POLICY_ALLOWED))
                )
            )
        return "policy", key
    if key in RESERVED_FIELD_NAMES:
        raise TeamError(
            "'{}' cannot be a room field (it's a local {} setting, not shared).".format(
                key, CONFIG_FILE
            )
        )
    if not CUSTOM_KEY_RE.match(key):
        raise TeamError(
            "bad field name '{}': use lowercase letters, digits, '_' or '-', "
            "1-32 chars, starting with a letter.".format(key)
        )
    if key in DEFAULTS:
        raise TeamError(
            "'{}' is a reserved config name; use policy.{} instead.".format(key, key)
        )
    return "custom", key


def validate_policy_value(name: str, raw: Any) -> Any:
    s = str(raw).strip()
    if name == "overlap_action":
        if s not in ("ask", "warn", "deny"):
            raise TeamError("policy.overlap_action must be one of ask|warn|deny")
        return s
    if name == "overlap_granularity":
        if s not in ("file", "dir"):
            raise TeamError("policy.overlap_granularity must be file or dir")
        return s
    if name in ("stale_after_min", "touch_ttl_min"):
        try:
            v = int(s)
        except ValueError:
            raise TeamError("policy.{} must be an integer".format(name))
        if not 1 <= v <= 1440:
            raise TeamError("policy.{} must be between 1 and 1440".format(name))
        return v
    if name in ("digest_interval_sec", "fetch_interval_sec"):
        try:
            v = int(s)
        except ValueError:
            raise TeamError("policy.{} must be an integer".format(name))
        if not 0 <= v <= 3600:
            raise TeamError("policy.{} must be between 0 and 3600".format(name))
        return v
    if name == "breaking_check":
        sv = s.lower()
        if sv in ("true", "on", "1"):
            return True
        if sv in ("false", "off", "0"):
            return False
        raise TeamError("policy.breaking_check must be true/false (or on/off, 1/0)")
    raise TeamError("unknown policy {}".format(name))


def validate_field_value(kind: str, full_key: str, raw: Any) -> Any:
    if kind == "policy":
        return validate_policy_value(full_key[len("policy."):], raw)
    if kind == "rule":
        raise TeamError("use 'room rule add' / 'room rule rm' for rule.* keys")
    limit = MAX_DESCRIPTION if full_key == "description" else MAX_TEXT
    return clean_text(str(raw), full_key, limit=limit)


def room_config_writers(ctx: "Ctx", room: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for _path, raw in ctx.store.list_files(ctx.store.tip(), config_prefix(room)).items():
        try:
            d = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("writer"):
            out[str(d["writer"])] = d
    return out


def load_config_doc(ctx: "Ctx", room: str, writer: str) -> Dict[str, Any]:
    raw = ctx.store.read_file(config_path(room, writer))
    doc = None
    if raw:
        try:
            doc = json.loads(raw.decode("utf-8"))
        except ValueError:
            doc = None
    if not isinstance(doc, dict):
        doc = {"schema": 1, "writer": writer, "room": room, "rev": 0, "seq": 0,
               "updated": now_iso(), "fields": {}}
    doc.setdefault("fields", {})
    doc.setdefault("seq", 0)
    return doc


def next_rule_seq(doc: Dict[str, Any]) -> int:
    doc["seq"] = int(doc.get("seq", 0)) + 1
    return doc["seq"]


def highest_clock_for_key(writers_docs: Dict[str, Dict[str, Any]], key: str) -> int:
    best = 0
    for doc in writers_docs.values():
        e = (doc.get("fields") or {}).get(key)
        if isinstance(e, dict):
            try:
                c = int(e.get("clock", 0))
            except (TypeError, ValueError):
                c = 0
            if c > best:
                best = c
    return best


def _set_field(doc: Dict[str, Any], writers_docs: Dict[str, Dict[str, Any]], key: str,
               value: Any) -> None:
    clock = highest_clock_for_key(writers_docs, key) + 1
    doc.setdefault("fields", {})[key] = {"value": value, "clock": clock, "at": now_iso()}


def effective_config(ctx: "Ctx", room: str) -> Dict[str, Dict[str, Any]]:
    """Per key, the entry with the highest (clock, at, writer). `goal` also has a
    base layer at clock 0 from the legacy room.json, so a v0.1-only room still
    shows its goal, and any room-config write to `goal` (clock >= 1) outranks it."""
    writers_docs = room_config_writers(ctx, room)
    keys: Dict[str, Dict[str, Any]] = {}
    meta = ctx.room_meta(room)
    if meta.get("goal"):
        keys["goal"] = {"value": meta["goal"], "clock": 0,
                        "at": str(meta.get("updated") or ""),
                        "writer": meta.get("updated_by") or "room"}
    for writer, doc in writers_docs.items():
        for key, entry in (doc.get("fields") or {}).items():
            if not isinstance(entry, dict):
                continue
            try:
                clock = int(entry.get("clock", 0))
            except (TypeError, ValueError):
                clock = 0
            at = str(entry.get("at") or "")
            cand = (clock, at, writer)
            cur = keys.get(key)
            if cur is None or cand > (cur["clock"], cur["at"], cur["writer"]):
                keys[key] = {"value": entry.get("value"), "clock": clock, "at": at,
                            "writer": writer}
    return keys


def save_config_doc(ctx: "Ctx", room: str, writer: str, doc: Dict[str, Any], message: str,
                    touched_keys: Optional[set] = None) -> str:
    doc["rev"] = int(doc.get("rev", 0)) + 1
    doc["updated"] = now_iso()
    doc["writer"] = writer
    doc["room"] = room
    doc.setdefault("schema", 1)
    ctx.store.write({config_path(room, writer): dumps_doc(doc)}, message)
    if touched_keys and "goal" in touched_keys:
        eff = effective_config(ctx, room)
        entry = eff.get("goal")
        value = entry["value"] if entry else None
        rmeta = ctx.room_meta(room) or {"name": room, "created": now_iso()}
        rmeta["goal"] = value or ""
        rmeta["updated"] = now_iso()
        rmeta["updated_by"] = entry["writer"] if entry else writer
        ctx.store.write({room_path(room): dumps_doc(rmeta)}, "{}: mirror goal".format(writer))
    return ctx.store.sync(force=True)  # explicit actions publish at once, like other writes


def resolve_writer(ctx: "Ctx") -> str:
    if ctx.ident:
        if ctx.ident["role"] != "member":
            raise TeamError(
                "observers are read-only. Join as a member to edit room config: {} join {} "
                "--as <name>".format(CMD, ctx.ident["room"])
            )
        return ctx.ident["id"]
    return default_owner(ctx.repo)


def require_config_writer(ctx: "Ctx", room_arg: Optional[str]) -> Tuple[str, str]:
    """Default room = joined room; if not joined, --room is required."""
    writer = resolve_writer(ctx)
    if ctx.ident:
        room = slug(room_arg) if room_arg else ctx.ident["room"]
    else:
        if not room_arg:
            raise TeamError(
                "not joined: pass --room <room>, e.g. {} room set title \"...\" "
                "--room hackathon".format(CMD)
            )
        room = slug(room_arg)
    return room, writer


def resolve_show_room(ctx: "Ctx", room_arg: Optional[str]) -> str:
    if room_arg:
        return slug(room_arg)
    if ctx.ident:
        return ctx.ident["room"]
    raise TeamError("not joined: pass --room <room>")


def check_limits(ctx: "Ctx", room: str, new_keys: List[str], new_rule_count: int) -> None:
    eff = effective_config(ctx, room)
    active_custom = {k for k, v in eff.items() if v["value"] is not None and is_custom_key(k)}
    new_custom = {k for k in new_keys if is_custom_key(k) and k not in active_custom}
    if len(active_custom) + len(new_custom) > MAX_ACTIVE_CUSTOM_FIELDS:
        raise TeamError("{} active custom fields max per room; unset one first.".format(
            MAX_ACTIVE_CUSTOM_FIELDS))
    if new_rule_count:
        active_rules = sum(1 for k, v in eff.items() if v["value"] is not None
                           and k.startswith("rule."))
        if active_rules + new_rule_count > MAX_ACTIVE_RULES:
            raise TeamError("{} active rules max per room; remove one first.".format(
                MAX_ACTIVE_RULES))


def rule_label(key: str) -> str:
    m = re.match(r"^rule\.([a-z0-9-]+)\.r(\d+)$", key)
    return "{}#r{}".format(m.group(1), m.group(2)) if m else key


def validate_template(data: Any) -> Tuple[List[Tuple[str, Any]], List[str]]:
    """Validates the `room export`/`--from` template shape. Returns (pairs, rule_texts)
    ready to write. Raises on the first problem; nothing is written by this function."""
    if not isinstance(data, dict):
        raise TeamError("template must be a JSON object")
    unknown = set(data) - TEMPLATE_TOP_KEYS
    if unknown:
        raise TeamError("unknown template key(s): {}".format(", ".join(sorted(unknown))))
    pairs: List[Tuple[str, Any]] = []
    for simple in ("title", "description", "goal"):
        if data.get(simple):
            limit = MAX_DESCRIPTION if simple == "description" else MAX_TEXT
            pairs.append((simple, clean_text(str(data[simple]), simple, limit=limit)))
    rules_raw = data.get("rules") or []
    if not isinstance(rules_raw, list):
        raise TeamError("template 'rules' must be a list of strings")
    rule_texts = [clean_text(str(r), "rule") for r in rules_raw]
    policy_raw = data.get("policy") or {}
    if not isinstance(policy_raw, dict):
        raise TeamError("template 'policy' must be an object")
    for name, val in policy_raw.items():
        kind, full_key = classify_key("policy." + str(name))
        pairs.append((full_key, validate_field_value(kind, full_key, val)))
    fields_raw = data.get("fields") or {}
    if not isinstance(fields_raw, dict):
        raise TeamError("template 'fields' must be an object")
    for key, val in fields_raw.items():
        kind, full_key = classify_key(str(key))
        if kind != "custom":
            raise TeamError(
                "'{}' cannot be set through 'fields'; use title/description/goal/policy/"
                "rules instead".format(key)
            )
        pairs.append((full_key, validate_field_value(kind, full_key, val)))
    return pairs, rule_texts


def new_doc(ident: Dict[str, str]) -> Dict[str, Any]:
    t = now_iso()
    return {
        "schema": SCHEMA, "id": ident["id"], "owner": ident["owner"], "agent": ident["agent"],
        "room": ident["room"], "role": ident["role"], "state": "online", "status": "idle",
        "working_on": "", "blocked_on": None, "handoff": "", "branch": "", "worktree": "",
        "touching": [], "decisions": [], "changes": [], "asks": [], "replies": [],
        "learnings": [], "seq": 0, "rev": 0, "started": t, "updated": t, "heartbeat": t,
    }


def next_id(doc: Dict[str, Any], prefix: str) -> str:
    doc["seq"] = int(doc.get("seq", 0)) + 1
    return "{}{}".format(prefix, doc["seq"])


def is_active(doc: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    if doc.get("state") == "offline":
        return False
    a = age_sec(doc.get("heartbeat"))
    return a is not None and a <= float(cfg["stale_after_min"]) * 60


def presence(doc: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    if doc.get("state") == "offline":
        return "offline"
    return "online" if is_active(doc, cfg) else "stale"


def valid_touching(doc: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    ttl = float(cfg["touch_ttl_min"]) * 60
    out = []
    for t in doc.get("touching") or []:
        if not isinstance(t, dict) or not t.get("path"):
            continue
        if t.get("source") == "claim":
            out.append(t)
        else:
            a = age_sec(t.get("at"))
            if a is not None and a <= ttl:
                out.append(t)
    return out


def touch_matches(t: Dict[str, Any], rel: str, granularity: str) -> bool:
    p = str(t["path"])
    if t.get("source") == "claim" or p.endswith("/"):
        base = p.rstrip("/")
        return base == "" or rel == base or rel.startswith(base + "/")
    if rel == p:
        return True
    if granularity == "dir":
        d = os.path.dirname(p)
        return bool(d) and os.path.dirname(rel) == d
    return False


def prune_doc(doc: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    n = int(cfg["max_items"])
    doc["touching"] = valid_touching(doc, cfg)[-50:]
    for k in ("learnings", "decisions", "changes", "replies"):
        doc[k] = (doc.get(k) or [])[-n:]
    asks = doc.get("asks") or []
    open_ = [a for a in asks if a.get("status") == "open"]
    closed = [a for a in asks if a.get("status") != "open"][-10:]
    doc["asks"] = [a for a in asks if a in open_ or a in closed]


# ----------------------------------------------------------------- context


class Ctx:
    def __init__(self, cwd: Optional[str] = None):
        start = cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        self.repo = Repo(start)
        self.cfg = load_config(self.repo)
        self.store = Store(self.repo, self.cfg)
        self.ident = resolve_identity(self.repo, self.cfg)
        if self.ident:
            self.store.author = self.ident["id"]
            self._apply_room_policy()

    def _apply_room_policy(self) -> None:
        """Precedence: DEFAULTS < .team-sync.json < room policy < env. Only the
        room-policy layer is new here; env overrides (e.g. CLAUDE_TEAM_REMOTE)
        were already applied by load_config() and are not policy.* keys, so they
        keep the final word. One extra local ls-tree/cat-file read; no network."""
        try:
            eff = effective_config(self, self.ident["room"])
        except (GitError, OSError, ValueError):
            return
        for key, entry in eff.items():
            if not key.startswith("policy.") or entry.get("value") is None:
                continue
            name = key[len("policy."):]
            if name in POLICY_ALLOWED:
                self.cfg[name] = entry["value"]

    def require(self, member: bool = False) -> Dict[str, str]:
        if not self.ident:
            raise TeamError("this agent has not joined a room. Run: {} join <room> --as <name>"
                            .format(CMD))
        if member and self.ident["role"] != "member":
            raise TeamError("observers are read-only. Join as a member to share status: "
                            "{} join {} --as <name>".format(CMD, self.ident["room"]))
        return self.ident

    def me_path(self) -> str:
        return agent_path(self.ident["room"], self.ident["id"])

    def load_me(self) -> Dict[str, Any]:
        raw = self.store.read_file(self.me_path())
        doc = None
        if raw:
            try:
                doc = json.loads(raw.decode("utf-8"))
            except ValueError:
                doc = None
        base = new_doc(self.ident)
        if not isinstance(doc, dict):
            return base
        for k, v in base.items():
            doc.setdefault(k, v)
        doc["role"] = self.ident["role"]
        return doc

    def save_me(self, doc: Dict[str, Any], message: str, content: bool = True,
                sync: bool = True, force_sync: bool = False) -> str:
        t = now_iso()
        doc["rev"] = int(doc.get("rev", 0)) + 1
        doc["heartbeat"] = t
        if content:
            doc["updated"] = t
        doc["branch"] = self.repo.current_branch()
        doc["worktree"] = os.path.basename(self.repo.root)
        doc["role"] = self.ident["role"]
        prune_doc(doc, self.cfg)
        self.store.write({self.me_path(): dumps_doc(doc)}, "{}: {}".format(self.ident["id"], message))
        return self.store.sync(force=force_sync) if sync else "not-synced"

    def docs(self, prefix: str) -> List[Dict[str, Any]]:
        out = []
        for path, raw in sorted(self.store.list_files(self.store.tip(), prefix).items()):
            if "/agents/" not in path or not path.endswith(".json"):
                continue
            try:
                d = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("id"):
                d["_presence"] = presence(d, self.cfg)
                out.append(d)
        return out

    def room_docs(self, room: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.docs("rooms/{}/agents/".format(room or self.ident["room"]))

    def all_docs(self) -> List[Dict[str, Any]]:
        return self.docs("rooms/")

    def room_meta(self, room: Optional[str] = None) -> Dict[str, Any]:
        raw = self.store.read_file(room_path(room or self.ident["room"]))
        try:
            d = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            d = {}
        return d if isinstance(d, dict) else {}

    # -- session state (per worktree, per Claude session)

    def session_path(self, sid: str) -> str:
        return os.path.join(self.repo.state_dir, "sessions", slug(sid, "default")[:64] + ".json")

    def load_session(self, sid: str) -> Optional[Dict[str, Any]]:
        d = read_json(self.session_path(sid))
        return d if isinstance(d, dict) else None

    def save_session(self, sid: str, sess: Dict[str, Any]) -> None:
        write_json(self.session_path(sid), sess)

    def cleanup_sessions(self, days: int = 3) -> None:
        d = os.path.join(self.repo.state_dir, "sessions")
        try:
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if time.time() - os.path.getmtime(p) > days * 86400:
                    os.remove(p)
        except OSError:
            pass


# ------------------------------------------------------------ overlap check


def find_overlaps(ctx: Ctx, rel: str) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    me = ctx.ident["id"] if ctx.ident else None
    out = []
    for doc in ctx.all_docs():
        if doc.get("id") == me or doc["_presence"] != "online":
            continue
        for t in valid_touching(doc, ctx.cfg):
            if touch_matches(t, rel, ctx.cfg["overlap_granularity"]):
                out.append((doc, t))
                break
    return out


def describe_overlap(doc: Dict[str, Any], t: Dict[str, Any]) -> str:
    how = "claimed" if t.get("source") == "claim" else "edited"
    return "{} ({} {} {}; working on: {})".format(
        doc["id"], how, t["path"], human_age(t.get("at")), doc.get("working_on") or "?")


def record_touch(doc: Dict[str, Any], rel: str) -> bool:
    for t in doc.get("touching") or []:
        if t.get("path") == rel and t.get("source") != "claim":
            a = age_sec(t.get("at"))
            if a is not None and a < 600:
                return False
            t["at"] = now_iso()
            return True
    doc.setdefault("touching", []).append({"path": rel, "at": now_iso(), "source": "auto"})
    return True


# --------------------------------------------------------- briefing/digest


def snapshot(doc: Dict[str, Any]) -> Dict[str, Any]:
    items = []
    for kind in ("decisions", "changes", "asks", "replies", "learnings"):
        for it in doc.get(kind) or []:
            items.append("{}:{}:{}".format(kind, it.get("id"), it.get("status", "")))
    return {
        "presence": doc.get("_presence"), "status": doc.get("status"),
        "working_on": doc.get("working_on"), "items": sorted(items),
    }


def push_error_info(repo: Repo) -> Optional[Dict[str, Any]]:
    """The last recorded push failure (local only), or None."""
    d = read_json(os.path.join(repo.shared_dir, PUSH_ERROR_FILE))
    return d if isinstance(d, dict) else None


def push_error_line(info: Dict[str, Any]) -> str:
    return (
        "! Your team-sync updates are not reaching the team: pushing to {} failed ({}) "
        "since {}. They stay on this laptop and are sent automatically once pushing "
        "works. Tell your human in one line; usually they need write (Developer) access "
        "to the repo."
    ).format(info.get("remote"), info.get("reason"), human_age(info.get("at")))


def reply_hint(aid: str, item_id: str) -> str:
    return '{} reply {}#{} "<answer>" [--done]'.format(CMD, aid, item_id)


def config_digest(ctx: Ctx, sess: Dict[str, Any]) -> List[str]:
    """Lines for room config (title/description/goal/rules/policy/fields) changes
    since this session last looked, each once."""
    room = ctx.ident["room"]
    eff = effective_config(ctx, room)
    seen = sess.setdefault("cfg_seen", {})
    lines: List[str] = []
    for key in sorted(set(eff) | set(seen)):
        cur = eff.get(key)
        cur_val = cur["value"] if cur else None
        cur_writer = cur["writer"] if cur else None
        prev = seen.get(key)
        if prev == [cur_val, cur_writer] or prev == (cur_val, cur_writer):
            continue
        seen[key] = [cur_val, cur_writer]
        if cur_val is None:
            if prev and prev[0] is not None:
                lines.append("ROOM: {} removed (by {})".format(key, prev[1]))
            continue
        if key.startswith("rule."):
            lines.append("ROOM RULE: {} (by {})".format(cur_val, cur_writer))
        elif key.startswith("policy."):
            lines.append("ROOM POLICY: {}={} (by {})".format(
                key[len("policy."):], cur_val, cur_writer))
        else:
            lines.append("ROOM: {} = {} (by {})".format(key, cur_val, cur_writer))
    return lines


def digest(ctx: Ctx, sess: Dict[str, Any]) -> List[str]:
    """Lines describing what changed in the room since this session last looked."""
    me = ctx.ident["id"]
    observer = ctx.ident["role"] == "observer"
    seen = sess.setdefault("seen", {})
    handoff_seen = sess.setdefault("handoff_seen", {})
    shown = set(sess.get("breaking_shown") or [])  # already surfaced by the Stop hook
    lines: List[str] = config_digest(ctx, sess)
    perr = push_error_info(ctx.repo)
    if perr and sess.get("push_error_seen") != perr.get("at"):
        lines.append(push_error_line(perr))
        sess["push_error_seen"] = perr.get("at")
    notice = _never_raise(self_update_notice)
    if notice:
        lines.append(notice)
    for doc in ctx.room_docs():
        aid = doc["id"]
        if aid == me:
            continue
        handoff_val = doc.get("handoff") or ""
        if handoff_val and handoff_seen.get(aid) != handoff_val:
            lines.append("HANDOFF from {}: {}".format(aid, handoff_val))
        handoff_seen[aid] = handoff_val
        snap = snapshot(doc)
        prev = seen.get(aid)
        seen[aid] = snap
        if prev == snap:
            continue
        if prev is None:
            if snap["presence"] == "online" and not observer:
                lines.append("+ {} joined ({}): {}".format(
                    aid, doc.get("role", "member"), doc.get("working_on") or "no task set yet"))
            prev = {"items": [], "presence": snap["presence"], "status": snap["status"],
                    "working_on": snap["working_on"]}
        if not observer:
            if prev.get("presence") != snap["presence"]:
                lines.append({"offline": "- {} went offline", "stale": "~ {} looks stale (no heartbeat)",
                              "online": "+ {} is back online"}[snap["presence"]].format(aid))
            if prev.get("status") != snap["status"]:
                extra = ""
                if snap["status"] == "blocked" and doc.get("blocked_on"):
                    extra = " (blocked on: {})".format(doc["blocked_on"])
                lines.append("* {}: {} -> {}{}".format(aid, prev.get("status"), snap["status"], extra))
            if prev.get("working_on") != snap["working_on"] and snap["working_on"]:
                lines.append("* {} now working on: {}".format(aid, snap["working_on"]))
        new = set(snap["items"]) - set(prev.get("items") or [])
        if not new:
            continue

        def is_new(kind: str, it: Dict[str, Any]) -> bool:
            return "{}:{}:{}".format(kind, it.get("id"), it.get("status", "")) in new

        for it in doc.get("changes") or []:
            if it.get("breaking") and "{}|changes:{}:{}".format(
                    aid, it.get("id"), it.get("status", "")) in shown:
                continue
            if is_new("changes", it) and (it.get("breaking") or not observer):
                if it.get("kind") == "ready":
                    lines.append("* READY from {}: {}".format(aid, it.get("text")))
                else:
                    tag = "!! BREAKING CHANGE" if it.get("breaking") else "> CHANGE"
                    lines.append("{} by {}: {}".format(tag, aid, it.get("text")))
        for it in doc.get("decisions") or []:
            if is_new("decisions", it):
                lines.append("> DECISION by {}: {}".format(aid, it.get("text")))
        if observer:
            continue
        for it in doc.get("asks") or []:
            if is_new("asks", it) and it.get("status") == "open" and it.get("to") in (me, "all"):
                lines.append("! ASK for you from {} [{}#{}]: {}  -> {}".format(
                    aid, aid, it.get("id"), it.get("text"), reply_hint(aid, it.get("id"))))
        for it in doc.get("replies") or []:
            if is_new("replies", it) and str(it.get("ask", "")).startswith(me + "#"):
                lines.append("! {} replied to your ask {}: {}{}".format(
                    aid, it.get("ask"), it.get("text"), " (done)" if it.get("done") else ""))
        for it in doc.get("learnings") or []:
            if is_new("learnings", it):
                lines.append("> LEARNING from {}: {}".format(aid, it.get("text")))
    # every agent's items are in `seen` now, so the Stop hook's list can be dropped
    sess["breaking_shown"] = []
    if len(lines) > MAX_DIGEST_LINES:
        extra = len(lines) - MAX_DIGEST_LINES
        lines = lines[:MAX_DIGEST_LINES] + ["... and {} more. Run: {} status".format(extra, CMD)]
    return lines


def breaking_digest(ctx: Ctx, sess: Dict[str, Any]) -> List[str]:
    """For the Stop hook: breaking changes this session has not been shown yet.
    Reuses digest()'s per-agent `seen` snapshot (so the next real digest() never
    repeats these), but marks *only* the breaking-change items as seen, leaving
    every other kind (asks, decisions, non-breaking changes, replies, learnings)
    unseen for the next prompt's digest()."""
    me = ctx.ident["id"]
    seen = sess.setdefault("seen", {})
    shown = set(sess.get("breaking_shown") or [])
    lines: List[str] = []
    for doc in ctx.room_docs():
        aid = doc["id"]
        if aid == me:
            continue
        prev_items = set((seen.get(aid) or {}).get("items") or [])
        for it in doc.get("changes") or []:
            if not it.get("breaking"):
                continue
            key = "changes:{}:{}".format(it.get("id"), it.get("status", ""))
            if key in prev_items or "{}|{}".format(aid, key) in shown:
                continue
            lines.append("{}: {}".format(aid, it.get("text")))
            shown.add("{}|{}".format(aid, key))
    # Kept apart from `seen`, so the next digest() still reports everything else
    # about these agents (e.g. "+ x joined") and only skips the items shown here.
    sess["breaking_shown"] = sorted(shown)
    return lines


def _breaking_key(aid: str, it: Dict[str, Any]) -> str:
    return "{}|changes:{}:{}".format(aid, it.get("id"), it.get("status", ""))


def reported_breaking(sess: Optional[Dict[str, Any]]) -> Set[str]:
    """Change items this session was already shown (briefing, digest or Stop), as
    `_breaking_key`s. Used so a resumed or compacted session doesn't warn twice."""
    out: Set[str] = set(((sess or {}).get("breaking_shown")) or [])
    for aid, snap in ((sess or {}).get("seen") or {}).items():
        for key in (snap or {}).get("items") or []:
            if key.startswith("changes:"):
                out.add("{}|{}".format(aid, key))
    return out


def mark_all_seen(ctx: Ctx, sess: Dict[str, Any]) -> None:
    seen = sess.setdefault("seen", {})
    handoff_seen = sess.setdefault("handoff_seen", {})
    for doc in ctx.room_docs():
        if doc["id"] != ctx.ident["id"]:
            seen[doc["id"]] = snapshot(doc)
            handoff_seen[doc["id"]] = doc.get("handoff") or ""
    cfg_seen = sess.setdefault("cfg_seen", {})
    for key, entry in effective_config(ctx, ctx.ident["room"]).items():
        cfg_seen[key] = [entry["value"], entry["writer"]]


def format_agent(doc: Dict[str, Any], me: Optional[str], cfg: Dict[str, Any],
                 indent: str = "  ") -> List[str]:
    mark = {"online": "*", "offline": "-", "stale": "?"}[doc["_presence"]]
    you = " (you)" if doc["id"] == me else ""
    head = "{}{} {}{} [{}{}] {}".format(
        indent, mark, doc["id"], you, doc.get("status", "idle"),
        ", observer" if doc.get("role") == "observer" else "",
        doc.get("working_on") or "-")
    out = [head + "  ({})".format(
        doc["_presence"] if doc["_presence"] != "online" else human_age(doc.get("heartbeat")))]
    sub = indent + "    "
    if doc.get("blocked_on"):
        out.append(sub + "BLOCKED on: {}".format(doc["blocked_on"]))
    touching = valid_touching(doc, cfg)
    if touching and doc["_presence"] == "online":
        out.append(sub + "touching: " + ", ".join(
            t["path"] + (" (claim)" if t.get("source") == "claim" else "") for t in touching[-6:]))
    ready_items = [c for c in doc.get("changes") or [] if c.get("kind") == "ready"]
    if ready_items:
        latest = max(ready_items, key=lambda c: str(c.get("at", "")))
        if within(latest.get("at"), 86400):
            out.append(sub + "ready: {}".format(latest.get("text")))
    for a in doc.get("asks") or []:
        if a.get("status") == "open":
            out.append(sub + "open ask -> {} [{}#{}]: {}".format(a.get("to"), doc["id"], a.get("id"), a.get("text")))
    if doc.get("handoff") and doc.get("status") == "done":
        out.append(sub + "handoff: {}".format(doc["handoff"]))
    return out


def briefing(ctx: Ctx, reported: Optional[Set[str]] = None) -> str:
    ident = ctx.ident
    me = ident["id"]
    room = ident["room"]
    docs = ctx.room_docs()
    others = [d for d in docs if d["id"] != me]
    active = [d for d in others if d["_presence"] == "online"]
    stale = [d for d in others if d["_presence"] == "stale"]
    L = ["[team-sync] You are agent \"{}\" (owner: {}) in room \"{}\" as {}.".format(
        me, ident["owner"], room, ident["role"])]
    perr = push_error_info(ctx.repo)
    if perr:
        L.append(push_error_line(perr))
    notice = _never_raise(self_update_notice)
    if notice:
        L.append("[team-sync] " + notice)
    eff = effective_config(ctx, room)
    cfg_active = {k: v for k, v in eff.items() if v["value"] is not None}
    if "goal" in cfg_active:
        L.append("Room goal: {}".format(cfg_active["goal"]["value"]))
    if "title" in cfg_active:
        L.append("Room: {}".format(cfg_active["title"]["value"]))
    if "description" in cfg_active:
        L.append("About: {}".format(cfg_active["description"]["value"]))
    rules = sorted((k, v) for k, v in cfg_active.items() if k.startswith("rule."))
    if rules:
        L.append("Room rules (follow these):")
        L.extend(" - {} [{}]".format(v["value"], rule_label(k)) for k, v in rules)
    fields = sorted((k, v) for k, v in cfg_active.items() if is_custom_key(k))
    if fields:
        L.append("Room info:")
        L.extend("{}: {}".format(k, v["value"]) for k, v in fields)
    policies = sorted((k, v) for k, v in cfg_active.items() if k.startswith("policy."))
    for k, v in policies:
        L.append("Room policy: {}={} (set by {})".format(k[len("policy."):], v["value"], v["writer"]))
    if ident["role"] == "observer":
        L.append("Observer mode: you do not report status. Your file edits are recorded so other "
                 "agents can avoid collisions, and you will be warned before editing an area "
                 "someone else is working on.")
    L.append("Active agents: {}{}".format(
        len(active), " ({} stale, ignored)".format(len(stale)) if stale else ""))
    for d in active:
        L.extend(format_agent(d, me, ctx.cfg))
    inactive_handoffs = [d for d in others if d["_presence"] != "online" and d.get("handoff")
                         and within(d.get("updated"), 86400)]
    if inactive_handoffs:
        L.append("Handoffs from agents no longer active:")
        L.extend("  - {} ({}): {}".format(d["id"], d["_presence"], d["handoff"])
                 for d in inactive_handoffs)
    if ident["role"] == "member":
        asks = [(d["id"], a) for d in others for a in d.get("asks") or []
                if a.get("status") == "open" and a.get("to") in (me, "all")]
        for aid, a in asks:
            L.append("! Open ASK for you from {} [{}#{}]: {}  -> tell your human with a proposed "
                     "answer; after they agree: reply {}#{} \"<answer>\" [--done]".format(
                aid, aid, a.get("id"), a.get("text"), aid, a.get("id")))
        mine = ctx.load_me()
        if mine.get("handoff") and mine.get("status") != "done":
            L.append("Your last handoff note: {}".format(mine["handoff"]))
    decisions = [(d["id"], x) for d in docs for x in d.get("decisions") or []]
    decisions.sort(key=lambda p: str(p[1].get("at", "")))
    if decisions:
        L.append("Team decisions (follow these):")
        L.extend("  - {} ({})".format(x.get("text"), aid) for aid, x in decisions[-8:])
    breaking = [(d["id"], x) for d in others for x in d.get("changes") or []
                if x.get("breaking") and within(x.get("at"), 86400)]
    # on resume/compact the same session already warned its human about some of these
    done = reported or set()
    fresh = [(aid, x) for aid, x in breaking if _breaking_key(aid, x) not in done]
    old = [(aid, x) for aid, x in breaking if _breaking_key(aid, x) in done]
    if fresh:
        L.append("Breaking changes in the last 24h (check if your code is affected):")
        L.extend("  - {} ({})".format(x.get("text"), aid) for aid, x in fresh)
        L.append(BREAKING_IMPACT_INSTR_SHORT)
    if old:
        L.append("Breaking changes already reported to your human in this session (don't repeat them): {}".format(
            "; ".join("{} ({})".format(x.get("text"), aid) for aid, x in old)))
    if ident["role"] == "member":
        ready = [(d["id"], x) for d in others for x in d.get("changes") or []
                if x.get("kind") == "ready" and within(x.get("at"), 7 * 86400)]
        ready.sort(key=lambda p: str(p[1].get("at", "")), reverse=True)
        if ready:
            L.append("Available from the team (announced):")
            L.extend("  - {} ({}, {})".format(x.get("text"), aid, human_age(x.get("at")))
                     for aid, x in ready[:8])
        learn = [(d["id"], x) for d in others for x in d.get("learnings") or []]
        learn.sort(key=lambda p: str(p[1].get("at", "")))
        if learn:
            L.append("Recent learnings from the team:")
            L.extend("  - {} ({})".format(x.get("text"), aid) for aid, x in learn[-6:])
        L.append("")
        L.append("Team CLI: {} <command>. Use the team-sync:team-protocol skill for the full rules. Essentials:".format(CMD))
        L.append('- When you start a task: update --status in_progress --working-on "<task>"')
        L.append('- When blocked: update --blocked-on "<what>". When done: update --status done --handoff "<state + next steps>"')
        L.append('- Share: learn "<text>" | decide "<text>" | change "<text>" [--breaking] | '
                 'announce "<what is ready + how to use it>" | ask <agent-id|all> "<text>"')
        L.append('- Answer an ask (propose to your human first, then reply; never answer with a new ask): '
                 'reply <agent-id>#<ask-id> "<answer>" [--done]')
        L.append("- Your edits are recorded automatically. Claim a whole area before a big refactor: claim <path/>")
        L.append("- Run each team command on its own (no &&, ;, pipes or redirects) so it needs no "
                 "permission prompt; avoid $ and backticks in the text.")
        if ctx.cfg.get("overlap_action") == "warn":
            overlap_rule = ("on an overlap warning, tell your human in one line and keep going with a minimal "
                            "change (stop and ask only before refactoring that area)")
        else:
            overlap_rule = "on an overlap, stop and ask your human"
        breaking_rule = ("for every breaking change, start your answer with a one-line warning plus a "
                         "quick impact check, and propose (don't apply) the fix")
        ask_rule = "propose answers to asks addressed to you, reply after your human agrees"
        L.append("Reaction rules: apply learnings silently; follow decisions; {}; {}; {}; never edit "
                 "another agent's file.".format(overlap_rule, breaking_rule, ask_rule))
    return "\n".join(L)


# ------------------------------------------------------------------- hooks


def hook_output(event: str, context: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"hookEventName": event}
    if context:
        out["additionalContext"] = context
    out.update(extra)
    return {"hookSpecificOutput": out}


def maybe_heartbeat(ctx: Ctx) -> None:
    if ctx.ident["role"] not in ROLES:
        return
    doc = ctx.load_me()
    a = age_sec(doc.get("heartbeat"))
    if doc.get("state") == "offline" or a is None or a > float(ctx.cfg["heartbeat_interval_sec"]):
        content = doc.get("state") == "offline"
        doc["state"] = "online"
        ctx.save_me(doc, "heartbeat", content=content, sync=False)


# ------------------------------------------------------------- self-update
#
# The one network path outside Store.sync(): keeps a marketplace install of
# this plugin current when Claude Code's own `/plugin` auto-update is off (the
# common case for npm installs or "autoUpdates": false). Fully inert unless
# the running script sits inside a marketplace plugin cache - any other
# layout (--plugin-dir, this repo, tests that don't build that layout) is a
# no-op with no output and no files. See docs/DESIGN.md.


def self_update_paths(config_dir: str) -> Tuple[str, str]:
    d = os.path.join(config_dir, "team-sync")
    return os.path.join(d, "update.json"), os.path.join(d, "update.lock")


def detect_marketplace_install() -> Optional[Tuple[str, str, str]]:
    """(config_dir, marketplace, version) if this script is running from
    <config_dir>/plugins/cache/<marketplace>/team-sync/<version>/scripts/team.py,
    else None. config_dir and marketplace are derived from the path itself, so
    this also picks up a non-default CLAUDE_CONFIG_DIR without reading it."""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root = os.path.realpath(root)
    version_dir = root
    version = os.path.basename(version_dir)
    team_sync_dir = os.path.dirname(version_dir)
    if os.path.basename(team_sync_dir) != "team-sync":
        return None
    marketplace_dir = os.path.dirname(team_sync_dir)
    marketplace = os.path.basename(marketplace_dir)
    cache_dir = os.path.dirname(marketplace_dir)
    if os.path.basename(cache_dir) != "cache":
        return None
    plugins_dir = os.path.dirname(cache_dir)
    if os.path.basename(plugins_dir) != "plugins":
        return None
    config_dir = os.path.dirname(plugins_dir)
    if not marketplace or not version:
        return None
    return config_dir, marketplace, version


def _version_tuple(v: Any) -> Tuple[int, ...]:
    out = []
    for part in str(v).split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)


def _read_update_info(path: str) -> Dict[str, Any]:
    d = read_json(path)
    return dict(d) if isinstance(d, dict) else {}


def _acquire_update_lock(lock_path: str) -> bool:
    """O_EXCL lock, stale after 10 min (a crashed/killed self-update)."""
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                stale = (time.time() - os.path.getmtime(lock_path)) > 600
            except OSError:
                stale = True
            if not stale:
                return False
            try:
                os.remove(lock_path)
            except OSError:
                pass
    return False


def _release_update_lock(lock_path: str) -> None:
    try:
        os.remove(lock_path)
    except OSError:
        pass


_SELF_UPDATE_DROP_ENV = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID"}


def _claude_child_env() -> Dict[str, str]:
    """Env for a nested `claude` call: drop variables that identify/configure
    the current Claude Code session (which could confuse a nested CLI); keep
    CLAUDE_CONFIG_DIR, HOME, PATH and everything else."""
    env = dict(os.environ)
    for k in list(env):
        if k in _SELF_UPDATE_DROP_ENV or k.startswith("CLAUDE_CODE_"):
            del env[k]
    return env


def run_self_update() -> None:
    """`team self-update`: checks the marketplace for a newer team-sync and
    updates it. Never raises - every outcome, including "not applicable" cases
    that return early, is recorded in update.json (except when this isn't a
    marketplace install at all, or the lock is held: then nothing happens)."""
    install = detect_marketplace_install()
    if not install:
        return
    config_dir, marketplace, version = install
    mode = os.environ.get("TEAM_SYNC_AUTO_UPDATE", "on").strip().lower()
    if mode == "off":
        return
    update_path, lock_path = self_update_paths(config_dir)
    if not _acquire_update_lock(lock_path):
        return
    info = _read_update_info(update_path)
    try:
        info["checked_at"] = now_iso()
        info["running"] = version
        claude = shutil.which("claude")
        if not claude:
            info["error"] = "claude executable not found on PATH"
            return
        env = _claude_child_env()
        p = subprocess.run([claude, "plugin", "marketplace", "update", marketplace], env=env,
                           timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0:
            info["error"] = "marketplace update failed: {}".format(
                _first_stderr_line(p.stderr.decode("utf-8", "replace")))
            return
        known = read_json(os.path.join(config_dir, "plugins", "known_marketplaces.json"))
        mp_entry = known.get(marketplace) if isinstance(known, dict) else None
        install_location = mp_entry.get("installLocation") if isinstance(mp_entry, dict) else None
        if not install_location:
            info["error"] = "marketplace '{}' has no known install location".format(marketplace)
            return
        mp_meta = read_json(os.path.join(install_location, ".claude-plugin", "marketplace.json"))
        plugin_entry = None
        for entry in (mp_meta.get("plugins") or []) if isinstance(mp_meta, dict) else []:
            if isinstance(entry, dict) and entry.get("name") == "team-sync":
                plugin_entry = entry
                break
        if not plugin_entry:
            info["error"] = "team-sync not listed in the marketplace"
            return
        source = plugin_entry.get("source") or "./plugins/team-sync"
        source_dir = source if os.path.isabs(source) else os.path.join(install_location, source)
        plugin_meta = read_json(os.path.join(source_dir, ".claude-plugin", "plugin.json"))
        available = plugin_meta.get("version") if isinstance(plugin_meta, dict) else None
        if not available:
            info["error"] = "could not read team-sync's version from the marketplace"
            return
        info["available"] = available
        info.pop("error", None)
        if mode == "notify":
            if _version_tuple(available) > _version_tuple(version):
                info["error"] = "TEAM_SYNC_AUTO_UPDATE=notify: not applying automatically"
            return
        if _version_tuple(available) <= _version_tuple(version):
            return
        p2 = subprocess.run([claude, "plugin", "update", "team-sync@{}".format(marketplace)],
                            env=env, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p2.returncode == 0:
            info["updated_to"] = available
            info.pop("error", None)
        else:
            info["error"] = "plugin update failed: {}".format(
                _first_stderr_line(p2.stderr.decode("utf-8", "replace")))
    except Exception as e:  # noqa: BLE001 - self-update must never raise
        info["error"] = "unexpected error: {!r}".format(e)
    finally:
        try:
            write_json(update_path, info)
        except OSError:
            pass
        _release_update_lock(lock_path)


def _never_raise(fn: Any) -> Any:
    """Self-update is a side feature: a failure in it must never cost a session its
    briefing or digest, so its call sites swallow every error (logged nowhere: the
    next day's check simply tries again)."""
    try:
        return fn()
    except Exception:
        return None


def maybe_trigger_self_update() -> None:
    """Called once from begin_session, after this session's own briefing/digest
    text is already built, so a background update never shows up in the same
    session that triggered it. Spawns a detached `team self-update` at most
    once per 24h (tracked in update.json); does no network itself."""
    mode = os.environ.get("TEAM_SYNC_AUTO_UPDATE", "on").strip().lower()
    if mode == "off":
        return
    install = detect_marketplace_install()
    if not install:
        return
    config_dir, _marketplace, _version = install
    update_path, _lock_path = self_update_paths(config_dir)
    info = read_json(update_path)
    if isinstance(info, dict) and within(info.get("checked_at"), 86400):
        return
    try:
        new_info = dict(info) if isinstance(info, dict) else {}
        new_info["checked_at"] = now_iso()
        write_json(update_path, new_info)
    except OSError:
        return
    argv = ["python3", os.path.abspath(__file__), "self-update"]
    try:
        if os.environ.get("TEAM_SYNC_UPDATE_SYNC"):
            subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=130)
        else:
            kwargs: Dict[str, Any] = {}
            if hasattr(os, "setsid"):
                kwargs["start_new_session"] = True
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **kwargs)
    except OSError:
        pass


def self_update_notice() -> Optional[str]:
    """Line for the briefing or the next prompt's digest about a background
    self-update, or None. Mutates update.json so the "updated" notice is shown
    once (cleared silently once the running VERSION catches up) and the
    "failed but available" notice is shown at most once per 24h."""
    install = detect_marketplace_install()
    if not install:
        return None
    config_dir, marketplace, _version = install
    update_path, _lock_path = self_update_paths(config_dir)
    info = read_json(update_path)
    if not isinstance(info, dict):
        return None
    changed = False
    line = None
    updated_to = info.get("updated_to")
    if updated_to:
        if _version_tuple(VERSION) < _version_tuple(updated_to):
            if not info.get("notified_updated"):
                line = (
                    "team-sync was updated from {} to {} in the background; it's active from "
                    "your next Claude Code start. Tell your human in one line."
                ).format(info.get("running") or VERSION, updated_to)
                info["notified_updated"] = True
                changed = True
        else:
            if info.get("notified_updated") or "updated_to" in info:
                info.pop("updated_to", None)
                info.pop("notified_updated", None)
                changed = True
    if line is None:
        available = info.get("available")
        error = info.get("error")
        if available and error and _version_tuple(available) > _version_tuple(VERSION):
            if not within(info.get("notified_failed_at"), 86400):
                line = (
                    "team-sync {} is available (you have {}), the automatic update failed ({}). "
                    "Tell your human to run: claude plugin marketplace update {}, then claude "
                    "plugin update team-sync@{}, then restart Claude Code."
                ).format(available, VERSION, error, marketplace, marketplace)
                info["notified_failed_at"] = now_iso()
                changed = True
    if changed:
        write_json(update_path, info)
    return line


def self_update_doctor_info() -> Dict[str, Any]:
    install = detect_marketplace_install()
    if not install:
        return {"marketplace_install": None, "update": None}
    config_dir, marketplace, version = install
    update_path, _lock_path = self_update_paths(config_dir)
    return {
        "marketplace_install": {"config_dir": config_dir, "marketplace": marketplace, "version": version},
        "update": read_json(update_path),
    }


def begin_session(ctx: Ctx, sid: str) -> Tuple[Dict[str, Any], str]:
    """Mark this agent online, publish it, and build the briefing. Run by SessionStart,
    and by the first prompt when SessionStart never fired for this session (a plugin
    enabled through project settings is loaded after the session has started)."""
    doc = ctx.load_me()
    doc["state"] = "online"
    # write first, then one forced sync: publishes us and pulls everyone else
    ctx.save_me(doc, "session start", content=True, sync=True, force_sync=True)
    reported = reported_breaking(ctx.load_session(sid))  # resume/compact keep the session id
    sess = {"started": now_iso(), "seen": {}, "acks": [], "last_digest": now_iso()}
    text = briefing(ctx, reported)
    mark_all_seen(ctx, sess)
    ctx.save_session(sid, sess)
    ctx.cleanup_sessions()
    _never_raise(maybe_trigger_self_update)  # after `text` is built: never in this session's own briefing
    return sess, text


def h_session_start(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ctx = Ctx(data.get("cwd"))
    if not ctx.ident:
        return None
    _, text = begin_session(ctx, str(data.get("session_id") or "default"))
    return hook_output("SessionStart", text)


def _digest_text(ctx: Ctx, lines: List[str]) -> str:
    text = "[team-sync] Updates in room \"{}\":\n{}\nApply the team-protocol reaction rules.".format(
        ctx.ident["room"], "\n".join(lines))
    if any(l.startswith("!! BREAKING CHANGE") for l in lines):
        text += "\n\n" + BREAKING_IMPACT_INSTR
    if any(l.startswith("* READY") for l in lines):
        text += "\n\n" + READY_INSTR
    return text


def h_prompt(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ctx = Ctx(data.get("cwd"))
    if not ctx.ident:
        return None
    sid = str(data.get("session_id") or "default")
    sess = ctx.load_session(sid)
    if sess is None:  # SessionStart never ran: joined mid-session, or plugin loaded late
        sess, text = begin_session(ctx, sid)
    else:
        ctx.store.sync()
        maybe_heartbeat(ctx)
        lines = digest(ctx, sess)
        text = _digest_text(ctx, lines) if lines else None
    sess["last_digest"] = now_iso()
    sess["turn"] = {"started": now_iso(), "edits": [], "checked": False}
    ctx.save_session(sid, sess)
    return hook_output("UserPromptSubmit", text) if text else None


def _tool_path(data: Dict[str, Any]) -> Optional[str]:
    ti = data.get("tool_input") or {}
    return ti.get("file_path") or ti.get("notebook_path") or ti.get("path")


def h_pre_edit(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ctx = Ctx(data.get("cwd"))
    if not ctx.ident:
        return None
    rel = ctx.repo.rel(_tool_path(data) or "", base=data.get("cwd"))
    if not rel or rel.startswith(".git/"):
        return None
    overlaps = find_overlaps(ctx, rel)
    if not overlaps:
        return None
    sid = str(data.get("session_id") or "default")
    sess = ctx.load_session(sid) or {"seen": {}, "acks": []}
    acks = sess.setdefault("acks", [])
    keys = ["{}|{}".format(d["id"], t["path"]) for d, t in overlaps]
    fresh = [k for k in keys if k not in acks]
    who = "; ".join(describe_overlap(d, t) for d, t in overlaps)
    msg = "team-sync: {} overlaps with active work by {}.".format(rel, who)
    action = ctx.cfg.get("overlap_action", "ask")
    if action == "deny":
        return hook_output("PreToolUse", permissionDecision="deny",
                           permissionDecisionReason=msg + " Coordinate first (ask them via the team CLI), "
                           "or have your human change overlap_action.")
    if not fresh:
        return None  # already raised this session
    acks.extend(fresh)
    ctx.save_session(sid, sess)
    advice = ("Tell your human about the overlap and suggest coordinating (e.g. `ask <agent-id> ...`). "
              "If you proceed, keep the change minimal and mention it in your handoff.")
    if action == "ask":
        return hook_output("PreToolUse", advice, permissionDecision="ask",
                           permissionDecisionReason=msg + " Approve to edit anyway (asked once per area per session).")
    return hook_output("PreToolUse", msg + " " + advice)


def h_post_tool(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Records the edited path and throttled-syncs. No digest here (v0.3): updates
    arrive on the next prompt, or at the end of the turn for incoming breaking
    changes, via the Stop hook. On the first recorded edit of a turn, members
    (never observers) with breaking_check on get a one-time reminder to announce
    breaking changes before their final answer - this replaces a second message at
    Stop, so the agent's real answer is never duplicated or swallowed."""
    ctx = Ctx(data.get("cwd"))
    if not ctx.ident:
        return None
    rel = None
    if data.get("tool_name") in EDIT_TOOLS:
        rel = ctx.repo.rel(_tool_path(data) or "", base=data.get("cwd"))
        if rel and not rel.startswith(".git/"):
            doc = ctx.load_me()
            if record_touch(doc, rel) or doc.get("state") == "offline":
                doc["state"] = "online"
                ctx.save_me(doc, "touch " + rel, content=False, sync=False)
        else:
            rel = None
    ctx.store.sync()
    sid = str(data.get("session_id") or "default")
    sess = ctx.load_session(sid)
    if sess is None:
        return None
    first_edit = False
    if rel:
        turn = sess.setdefault(
            "turn", {"started": sess.get("started") or now_iso(), "edits": [], "checked": False})
        edits = turn.setdefault("edits", [])
        if not edits:
            first_edit = True
        if rel not in edits:
            edits.append(rel)
            del edits[:-50]
        ctx.save_session(sid, sess)
    if first_edit and ctx.ident["role"] == "member" and ctx.cfg.get("breaking_check", True):
        return hook_output("PostToolUse", POST_EDIT_BREAKING_REMINDER)
    return None


def h_stop(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """End-of-turn check, once per turn: surfaces breaking changes that arrived
    during the task (the first-edit reminder in h_post_tool covers the agent's own
    changes, so there's nothing else to add here). Returns a Stop-hook `block`
    with the text, or None."""
    ctx = Ctx(data.get("cwd"))
    if not ctx.ident:
        return None
    if data.get("stop_hook_active"):
        return None
    sid = str(data.get("session_id") or "default")
    sess = ctx.load_session(sid)
    if sess is None:
        return None
    turn = sess.get("turn")
    if not turn or turn.get("checked"):
        return None
    ctx.store.sync()
    parts: List[str] = []
    breaking_lines = breaking_digest(ctx, sess)
    if breaking_lines:
        parts.append("Incoming breaking changes:\n" + "\n".join(
            "- {}".format(l) for l in breaking_lines) + "\n\n" + STOP_INCOMING_INSTR)
    turn["checked"] = True
    sess["turn"] = turn
    ctx.save_session(sid, sess)
    if parts:
        return {"decision": "block", "reason": "[team-sync] " + "\n\n".join(parts)}
    return None


def h_session_end(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ctx = Ctx(data.get("cwd"))
    if not ctx.ident:
        return None
    doc = ctx.load_me()
    doc["state"] = "offline"
    ctx.save_me(doc, "session end", content=True, sync=True, force_sync=True)
    try:
        os.remove(ctx.session_path(str(data.get("session_id") or "default")))
    except OSError:
        pass
    return None


SHELL_META = set(";&|<>()$`\\")


def _has_shell_injection(args: str) -> bool:
    """Quote-aware scan of the arguments after the accepted script prefix: True
    means the shell could do something other than pass a literal argument
    (chaining, redirection, substitution, variables), so the call must not be
    auto-approved. Outside quotes, any SHELL_META char or a bare newline/CR
    rejects. Inside single quotes everything is literal. Inside double quotes
    only `$` and backtick are dangerous (the shell still expands them there),
    and a backslash escapes the next character. An unterminated quote at the
    end also rejects (shlex/the real shell would need more input)."""
    quote = None
    i = 0
    n = len(args)
    while i < n:
        ch = args[i]
        if quote is None:
            if ch in "'\"":
                quote = ch
            elif ch in SHELL_META or ch in "\n\r":
                return True
        elif quote == "'":
            if ch == "'":
                quote = None
        else:  # quote == '"'
            if ch == '"':
                quote = None
            elif ch == "\\":
                i += 1  # skip the escaped character
            elif ch in "$`":
                return True
        i += 1
    return quote is not None


def _room_cmd_needs_human(argv: List[str], cwd: Optional[str]) -> bool:
    """A `room` call that changes policy affects every agent in the room: a human
    must approve it, even though the rest of the CLI is auto-approved. A `room
    create ... --from <file>` bootstrap only needs approval when the template
    itself sets room policy (or can't be read/parsed at all, so we can't tell) -
    a plain template (title/description/goal/rules/fields, no policy) is treated
    like any other plain CLI call."""
    if not argv or argv[0] != "room":
        return False
    rest = argv[1:]
    if not rest:
        return False
    sub = rest[0]
    if sub in ("set", "unset") and len(rest) >= 2 and str(rest[1]).startswith("policy."):
        return True
    if sub == "create" and "--from" in rest:
        idx = rest.index("--from")
        path = rest[idx + 1] if idx + 1 < len(rest) else None
        if not path:
            return True
        full = path if os.path.isabs(path) else os.path.join(cwd or "", path)
        data = read_json(full)
        if not isinstance(data, dict):
            return True  # unreadable/unparsable: can't tell, ask
        if data.get("policy"):
            return True
        return False
    return False


def h_pre_bash(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Auto-approve plain calls of this CLI so agents can report without a prompt.

    Only the exact script path is accepted, and only when the arguments can't make
    the shell do anything but pass a literal string (see `_has_shell_injection`:
    quote-aware, so `;`, `|`, `&`, `<`, `>` etc. inside a quoted argument are fine,
    but chaining, redirection, substitution or variables outside quotes - or `$`/
    backtick even inside double quotes - are not), so nothing else can ride along.
    Everything else goes through normal permissions. A `room` call that touches
    policy (or bootstraps from a file) still needs an explicit human "ask":
    returning None is not enough, since the skills' allowed-tools would
    auto-approve it otherwise.
    """
    cmd = str((data.get("tool_input") or {}).get("command") or "").strip()
    script = os.path.abspath(__file__)
    for prefix in ('python3 "{}" '.format(script), "python3 '{}' ".format(script),
                   "python3 {} ".format(script)):
        if cmd.startswith(prefix):
            if _has_shell_injection(cmd[len(prefix):]):
                return None
            try:
                argv = shlex.split(cmd[len(prefix):])
            except ValueError:
                argv = []
            if _room_cmd_needs_human(argv, data.get("cwd")):
                return hook_output(
                    "PreToolUse", permissionDecision="ask",
                    permissionDecisionReason="team-sync: changing room policy affects every "
                    "agent in the room.")
            return hook_output("PreToolUse", permissionDecision="allow",
                               permissionDecisionReason="team-sync CLI")
    return None


def h_pre_skill(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Auto-approve loading this plugin's own skills (e.g. team-sync:team-protocol)
    once joined, so following the protocol never needs a permission prompt. No
    network, no filesystem access beyond the identity check in Ctx."""
    ctx = Ctx(data.get("cwd"))
    if not ctx.ident:
        return None
    ti = data.get("tool_input") or {}
    skill = ti.get("skill") or ti.get("name") or ti.get("command")
    if isinstance(skill, str) and skill.startswith("team-sync:"):
        return hook_output("PreToolUse", permissionDecision="allow",
                           permissionDecisionReason="team-sync skill")
    return None


HOOKS = {
    "pre-bash": h_pre_bash,
    "pre-skill": h_pre_skill,
    "session-start": h_session_start,
    "prompt": h_prompt,
    "pre-edit": h_pre_edit,
    "post-tool": h_post_tool,
    "stop": h_stop,
    "session-end": h_session_end,
}


def hook_main(event: str) -> int:
    """Hooks must never break the session: every failure exits 0 silently."""
    data: Dict[str, Any] = {}
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        data = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        data = {}
    try:
        out = HOOKS[event](data)
    except NotARepo:
        return 0
    except Exception as e:  # noqa: BLE001
        try:
            Repo(data.get("cwd") or os.getcwd()).log_error("hook {}: {!r}".format(event, e))
        except Exception:  # noqa: BLE001
            pass
        return 0
    if out:
        sys.stdout.write(json.dumps(out))
    return 0


# --------------------------------------------------------------------- CLI


def cmd_init(args: argparse.Namespace) -> int:
    repo = Repo(os.getcwd())
    path = os.path.join(repo.root, CONFIG_FILE)
    cfg = read_json(path) or {}
    cfg.setdefault("branch", args.branch or DEFAULTS["branch"])
    if args.branch:
        cfg["branch"] = args.branch
    if args.local:
        cfg["remote"] = None
    elif args.remote:
        cfg["remote"] = args.remote
    else:
        cfg.setdefault("remote", DEFAULTS["remote"])
    if args.auto_observe is not None:
        cfg["auto_observe"] = args.auto_observe or None
    write_json(path, cfg)
    ctx = Ctx(repo.root)
    if not ctx.store.tip():
        ctx.store.sync(force=True)
    if not ctx.store.tip():
        readme = (
            "# team-sync state\n\nThis branch is written by the team-sync Claude Code plugin.\n"
            "Each agent owns rooms/<room>/agents/<agent-id>.json. Do not edit by hand.\n"
        )
        ctx.store.write({"README.md": readme.encode()}, "team-sync: init")
        res = ctx.store.sync(force=True)
    else:
        res = "exists"
    print("Wrote {} and prepared branch '{}' ({}).".format(CONFIG_FILE, cfg["branch"], res))
    print("Next: commit {} to your main branch so every clone uses the same settings.".format(CONFIG_FILE))
    return 0


def cmd_join(args: argparse.Namespace) -> int:
    repo = Repo(os.getcwd())
    room = slug(args.room, "room")
    identity = {
        "room": room, "agent": slug(args.as_ or os.path.basename(repo.root)),
        "role": args.role, "owner": slug(args.owner, "dev") if args.owner else None,
        "joined": now_iso(),
    }
    identity = {k: v for k, v in identity.items() if v}
    write_json(os.path.join(repo.state_dir, "identity.json"), identity)
    ctx = Ctx(repo.root)
    if not ctx.ident:
        raise TeamError("CLAUDE_TEAM_ROOM=off is set in the environment; unset it to join.")
    if ctx.ident["source"] == "env" and ctx.ident["room"] != room:
        print("note: CLAUDE_TEAM_ROOM={} in the environment overrides the joined room.".format(
            ctx.ident["room"]), file=sys.stderr)
    ctx.store.sync(force=True)
    if not ctx.room_meta():
        meta = {"name": ctx.ident["room"], "goal": "", "created": now_iso(), "updated": now_iso()}
        ctx.store.write({room_path(ctx.ident["room"]): dumps_doc(meta)}, "create room")
    doc = ctx.load_me()
    doc["state"] = "online"
    res = ctx.save_me(doc, "join", force_sync=True)
    print("Joined room '{}' as {} ({}). Sync: {}.".format(
        ctx.ident["room"], ctx.ident["id"], ctx.ident["role"], res))
    _print_room(ctx)
    return 0


def cmd_leave(args: argparse.Namespace) -> int:
    ctx = Ctx()
    ident = ctx.require()
    doc = ctx.load_me()
    doc["state"] = "offline"
    ctx.save_me(doc, "leave", force_sync=True)
    try:
        os.remove(os.path.join(ctx.repo.state_dir, "identity.json"))
    except OSError:
        pass
    print("Left room '{}' ({} is now offline).".format(ident["room"], ident["id"]))
    if ident["source"] == "env":
        print("note: CLAUDE_TEAM_ROOM is set in the environment; unset it to stay out.")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    ctx = Ctx()
    if not ctx.ident:
        print("Not in a room.")
        return 1
    print(json.dumps(ctx.ident, indent=2))
    return 0


def _print_room(ctx: Ctx, room: Optional[str] = None) -> None:
    room = room or ctx.ident["room"]
    meta = ctx.room_meta(room)
    me = ctx.ident["id"] if ctx.ident else None
    age = ctx.store.last_sync_age()
    sync = "local only" if not ctx.store.remote_ok() else (
        "synced {}s ago".format(int(age)) if age is not None else "never synced")
    eff = effective_config(ctx, room)
    active = {k: v for k, v in eff.items() if v["value"] is not None}
    title_part = " \"{}\"".format(active["title"]["value"]) if "title" in active else ""
    print('Room "{}"{}{}  [branch {}, {}]'.format(
        room, title_part, " - goal: " + meta["goal"] if meta.get("goal") else "",
        ctx.store.branch, sync))
    if "description" in active:
        print("  {}".format(active["description"]["value"]))
    rules = sorted((k, v) for k, v in active.items() if k.startswith("rule."))
    if rules:
        print("  rules:")
        for k, v in rules:
            print("    - {} [{}]".format(v["value"], rule_label(k)))
    fields = sorted((k, v) for k, v in active.items() if is_custom_key(k))
    if fields:
        print("  fields: " + ", ".join("{}={}".format(k, v["value"]) for k, v in fields))
    policies = sorted((k, v) for k, v in active.items() if k.startswith("policy."))
    if policies:
        print("  policy: " + ", ".join(
            "{}={} (by {})".format(k[len("policy."):], v["value"], v["writer"])
            for k, v in policies))
    docs = ctx.room_docs(room)
    if not docs:
        print("  (no agents yet)")
    order = {"online": 0, "stale": 1, "offline": 2}
    for d in sorted(docs, key=lambda d: (order[d["_presence"]], d["id"])):
        for line in format_agent(d, me, ctx.cfg):
            print(line)


def list_rooms_info(ctx: Ctx) -> List[Dict[str, Any]]:
    """Every room with at least one file on the team branch (room.json, a config
    file, or an agent file), newest first by the latest `updated` seen in any
    of its files."""
    tip = ctx.store.tip()
    files = ctx.store.list_files(tip, "rooms/") if tip else {}
    updated: Dict[str, str] = {}
    for path, raw in files.items():
        parts = path.split("/")
        if len(parts) < 2 or parts[0] != "rooms":
            continue
        room = parts[1]
        updated.setdefault(room, "")
        try:
            d = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(d, dict):
            u = str(d.get("updated") or "")
            if u > updated[room]:
                updated[room] = u
    out = []
    for room in sorted(updated):
        docs = ctx.room_docs(room)
        online = sorted(d["id"] for d in docs if d["_presence"] == "online")
        eff = effective_config(ctx, room)
        title_entry = eff.get("title")
        goal_entry = eff.get("goal")
        out.append({
            "room": room,
            "title": title_entry["value"] if title_entry else None,
            "goal": goal_entry["value"] if goal_entry else None,
            "online": online,
            "agents": len(docs),
            "updated": updated[room] or None,
        })
    out.sort(key=lambda r: r["updated"] or "", reverse=True)
    return out


def cmd_rooms(args: argparse.Namespace) -> int:
    ctx = Ctx()
    if not args.no_sync:
        ctx.store.sync(force=True)
    rooms = list_rooms_info(ctx)
    if args.json:
        print(json.dumps(rooms, indent=2, ensure_ascii=False))
        return 0
    if not rooms:
        print("No rooms yet. A lead creates one with /team-sync:start <name>.")
        return 0
    for r in rooms:
        parts = [r["room"]]
        if r["title"]:
            parts.append('"{}"'.format(r["title"]))
        if r["goal"]:
            parts.append("goal: {}".format(r["goal"]))
        stats = "{} online".format(len(r["online"]))
        if r["online"]:
            stats += " ({})".format(", ".join(r["online"]))
        stats += ", {} total".format(r["agents"])
        parts.append("· " + stats)
        print("  ".join(parts))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ctx = Ctx()
    if not args.no_sync:
        ctx.store.sync(force=True)
    if args.json:
        docs = ctx.all_docs() if args.all else ctx.room_docs(args.room or ctx.require()["room"])
        print(json.dumps(docs, indent=2, ensure_ascii=False))
        return 0
    perr = push_error_info(ctx.repo)
    if perr:
        print(push_error_line(perr))
    if args.all:
        rooms = sorted({d["room"] for d in ctx.all_docs() if d.get("room")})
        for r in rooms or []:
            _print_room(ctx, r)
        if not rooms:
            print("No rooms yet.")
        return 0
    _print_room(ctx, args.room or ctx.require()["room"])
    return 0


def _mutate(args: argparse.Namespace, message: str, fn: Any, member: bool = True) -> int:
    ctx = Ctx()
    ctx.require(member=member)
    doc = ctx.load_me()
    result = fn(ctx, doc)
    res = ctx.save_me(doc, message, force_sync=True)  # explicit actions publish at once
    if result:
        print(result)
    print("ok (sync: {})".format(res))
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    def fn(ctx: Ctx, doc: Dict[str, Any]) -> None:
        if args.working_on is not None:
            doc["working_on"] = clean_text(args.working_on, "working_on") if args.working_on else ""
        if args.blocked_on:
            doc["blocked_on"] = clean_text(args.blocked_on, "blocked_on")
            doc["status"] = "blocked"
        if args.unblock:
            doc["blocked_on"] = None
            if doc.get("status") == "blocked":
                doc["status"] = "in_progress"
        if args.handoff is not None:
            doc["handoff"] = clean_text(args.handoff, "handoff") if args.handoff else ""
        if args.status:
            doc["status"] = args.status
            if args.status != "blocked" and not args.blocked_on:
                doc["blocked_on"] = None
        doc["state"] = "online"
    return _mutate(args, "update", fn)


def _add_item(list_name: str, prefix: str, **fields: Any) -> Any:
    def fn(ctx: Ctx, doc: Dict[str, Any]) -> str:
        item = {"id": next_id(doc, prefix), "at": now_iso()}
        item.update(fields)
        doc.setdefault(list_name, []).append(item)
        return "added {} {}".format(list_name[:-1], item["id"])
    return fn


def cmd_learn(args: argparse.Namespace) -> int:
    return _mutate(args, "learning", _add_item("learnings", "l", text=clean_text(args.text, "learning")))


def cmd_decide(args: argparse.Namespace) -> int:
    return _mutate(args, "decision", _add_item("decisions", "d", text=clean_text(args.text, "decision")))


def cmd_change(args: argparse.Namespace) -> int:
    return _mutate(args, "change", _add_item("changes", "c", text=clean_text(args.text, "change"),
                                            breaking=bool(args.breaking)))


def cmd_announce(args: argparse.Namespace) -> int:
    return _mutate(args, "announce", _add_item("changes", "c", text=clean_text(args.text, "announce"),
                                              breaking=False, kind="ready"))


def cmd_ask(args: argparse.Namespace) -> int:
    to = args.to if args.to == "all" else slug(args.to)
    text = clean_text(args.text, "ask")

    def fn(ctx: Ctx, doc: Dict[str, Any]) -> str:
        if to != "all" and to not in {d["id"] for d in ctx.room_docs()}:
            print("warning: no agent '{}' in room '{}' (yet).".format(to, ctx.ident["room"]), file=sys.stderr)
        return _add_item("asks", "a", to=to, text=text, status="open")(ctx, doc)
    return _mutate(args, "ask " + to, fn)


def cmd_reply(args: argparse.Namespace) -> int:
    if "#" not in args.ref:
        raise TeamError("reference must look like <agent-id>#<ask-id>, e.g. anna-api#a3")
    text = clean_text(args.text, "reply")
    return _mutate(args, "reply " + args.ref, _add_item("replies", "r", ask=args.ref, text=text,
                                                        done=bool(args.done)))


def cmd_close(args: argparse.Namespace) -> int:
    def fn(ctx: Ctx, doc: Dict[str, Any]) -> str:
        for a in doc.get("asks") or []:
            if a.get("id") == args.ask_id:
                a["status"] = "done"
                return "closed ask {}".format(args.ask_id)
        raise TeamError("you have no ask with id {}".format(args.ask_id))
    return _mutate(args, "close " + args.ask_id, fn)


def _claim_path(ctx: Ctx, p: str) -> str:
    rel = ctx.repo.rel(p, base=os.getcwd())
    if rel is None:
        raise TeamError("{} is outside the repository".format(p))
    full = os.path.join(ctx.repo.root, rel)
    if rel == "" or os.path.isdir(full) or p.endswith("/"):
        rel = rel.rstrip("/") + "/"
    return rel


def cmd_claim(args: argparse.Namespace) -> int:
    def fn(ctx: Ctx, doc: Dict[str, Any]) -> str:
        if args.note:
            clean_text(args.note, "note")
        added = []
        for p in args.paths:
            rel = _claim_path(ctx, p)
            if rel == "/":
                raise TeamError("refusing to claim the whole repository")
            for d, t in find_overlaps(ctx, rel.rstrip("/") or rel):
                print("warning: {} overlaps with {}".format(rel, describe_overlap(d, t)), file=sys.stderr)
            doc["touching"] = [t for t in doc.get("touching") or [] if t.get("path") != rel]
            doc["touching"].append({"path": rel, "at": now_iso(), "source": "claim",
                                    "note": args.note or ""})
            added.append(rel)
        return "claimed: " + ", ".join(added)
    return _mutate(args, "claim", fn, member=False)


def cmd_release(args: argparse.Namespace) -> int:
    def fn(ctx: Ctx, doc: Dict[str, Any]) -> str:
        if args.all:
            doc["touching"] = []
            return "released everything"
        targets = {_claim_path(ctx, p) for p in args.paths} | {
            ctx.repo.rel(p, base=os.getcwd()) for p in args.paths}
        before = len(doc.get("touching") or [])
        doc["touching"] = [t for t in doc.get("touching") or [] if t.get("path") not in targets]
        return "released {} entr{}".format(before - len(doc["touching"]),
                                           "y" if before - len(doc["touching"]) == 1 else "ies")
    if not args.all and not args.paths:
        raise TeamError("give paths to release, or --all")
    return _mutate(args, "release", fn, member=False)


def cmd_check(args: argparse.Namespace) -> int:
    ctx = Ctx()
    ctx.store.sync()
    found = 0
    for p in args.paths:
        rel = ctx.repo.rel(p, base=os.getcwd())
        if rel is None:
            continue
        for d, t in find_overlaps(ctx, rel.rstrip("/")):
            found += 1
            print("{}: overlaps with {}".format(rel, describe_overlap(d, t)))
    if not found:
        print("no overlaps")
    return 1 if found else 0


def cmd_sync(args: argparse.Namespace) -> int:
    ctx = Ctx()
    print(ctx.store.sync(force=True))
    return 0


def print_effective_config(ctx: Ctx, room: str, json_out: bool = False) -> None:
    eff = effective_config(ctx, room)
    active = {k: v for k, v in eff.items() if v["value"] is not None}
    if json_out:
        print(json.dumps({
            "room": room,
            "fields": {k: {"value": v["value"], "by": v["writer"], "at": v["at"]}
                      for k, v in active.items()},
        }, indent=2, ensure_ascii=False))
        return
    print('Room "{}" configuration:'.format(room))
    if not active:
        print("  (no config set)")
        return
    for simple in ("title", "description", "goal"):
        if simple in active:
            v = active[simple]
            print("  {}: {}  (by {}, {})".format(simple, v["value"], v["writer"], human_age(v["at"])))
    rules = sorted((k, v) for k, v in active.items() if k.startswith("rule."))
    if rules:
        print("  rules:")
        for k, v in rules:
            print("    - {}  [{}]".format(v["value"], rule_label(k)))
    policies = sorted((k, v) for k, v in active.items() if k.startswith("policy."))
    if policies:
        print("  policy:")
        for k, v in policies:
            print("    {} = {}  (by {})".format(k[len("policy."):], v["value"], v["writer"]))
    fields = sorted((k, v) for k, v in active.items() if is_custom_key(k))
    if fields:
        print("  fields:")
        for k, v in fields:
            print("    {}: {}  (by {})".format(k, v["value"], v["writer"]))


def cmd_room_show(args: argparse.Namespace) -> int:
    ctx = Ctx()
    goal_arg = getattr(args, "goal", None)
    room = resolve_show_room(ctx, args.room)
    if goal_arg is not None:  # legacy `room --goal T`: keep working, mirrors to room.json
        room, writer = require_config_writer(ctx, args.room or room)
        value = clean_text(goal_arg, "goal") if goal_arg else None
        doc = load_config_doc(ctx, room, writer)
        writers_docs = room_config_writers(ctx, room)
        _set_field(doc, writers_docs, "goal", value)
        res = save_config_doc(ctx, room, writer, doc, "{}: room goal".format(writer),
                              touched_keys={"goal"})
        print("ok (sync: {})".format(res))
    print_effective_config(ctx, room, json_out=bool(getattr(args, "json", False)))
    return 0


def cmd_room_create(args: argparse.Namespace) -> int:
    ctx = Ctx()
    room = slug(args.room)
    writer = resolve_writer(ctx)
    pairs: List[Tuple[str, Any]] = []
    rule_texts: List[str] = []
    if args.from_file:
        data = read_json(args.from_file)
        if data is None:
            raise TeamError("could not read template file: {}".format(args.from_file))
        pairs, rule_texts = validate_template(data)
    for flag, key in (("title", "title"), ("description", "description"), ("goal", "goal")):
        val = getattr(args, flag)
        if val is not None:
            limit = MAX_DESCRIPTION if key == "description" else MAX_TEXT
            value = clean_text(val, key, limit=limit) if val else None
            pairs = [p for p in pairs if p[0] != key] + [(key, value)]
    ctx.store.sync(force=True)
    if pairs or rule_texts:
        check_limits(ctx, room, [k for k, v in pairs if v is not None], len(rule_texts))
    # Everything is validated: only now touch the branch.
    created = not ctx.room_meta(room)
    if created:
        meta = {"name": room, "goal": "", "created": now_iso(), "updated": now_iso()}
        ctx.store.write({room_path(room): dumps_doc(meta)}, "{}: create room".format(writer))
    if pairs or rule_texts:
        doc = load_config_doc(ctx, room, writer)
        writers_docs = room_config_writers(ctx, room)
        touched = set()
        for key, value in pairs:
            _set_field(doc, writers_docs, key, value)
            touched.add(key)
        for text in rule_texts:
            n = next_rule_seq(doc)
            _set_field(doc, writers_docs, "rule.{}.r{}".format(writer, n), text)
        save_config_doc(ctx, room, writer, doc, "{}: create config".format(writer),
                        touched_keys=touched)
    elif created:
        ctx.store.sync(force=True)
    print("Room '{}' ready.".format(room))
    print_effective_config(ctx, room)
    return 0


def cmd_room_set(args: argparse.Namespace) -> int:
    ctx = Ctx()
    room, writer = require_config_writer(ctx, args.room)
    kind, full_key = classify_key(args.key)
    if kind == "rule":
        raise TeamError("use 'room rule add' / 'room rule rm' for rule.* keys")
    value = validate_field_value(kind, full_key, args.value)
    if kind == "custom":
        check_limits(ctx, room, [full_key], 0)
    doc = load_config_doc(ctx, room, writer)
    writers_docs = room_config_writers(ctx, room)
    _set_field(doc, writers_docs, full_key, value)
    res = save_config_doc(ctx, room, writer, doc, "{}: set {}".format(writer, full_key),
                          touched_keys={full_key})
    print("set {} = {} (sync: {})".format(full_key, value, res))
    return 0


def cmd_room_unset(args: argparse.Namespace) -> int:
    ctx = Ctx()
    room, writer = require_config_writer(ctx, args.room)
    kind, full_key = classify_key(args.key)
    if kind == "rule":
        raise TeamError("use 'room rule rm <writer>#r<n>' to remove a rule")
    doc = load_config_doc(ctx, room, writer)
    writers_docs = room_config_writers(ctx, room)
    _set_field(doc, writers_docs, full_key, None)
    res = save_config_doc(ctx, room, writer, doc, "{}: unset {}".format(writer, full_key),
                          touched_keys={full_key})
    print("unset {} (sync: {})".format(full_key, res))
    return 0


def cmd_room_rule_add(args: argparse.Namespace) -> int:
    ctx = Ctx()
    room, writer = require_config_writer(ctx, args.room)
    text = clean_text(args.text, "rule")
    check_limits(ctx, room, [], 1)
    doc = load_config_doc(ctx, room, writer)
    writers_docs = room_config_writers(ctx, room)
    n = next_rule_seq(doc)
    full_key = "rule.{}.r{}".format(writer, n)
    _set_field(doc, writers_docs, full_key, text)
    res = save_config_doc(ctx, room, writer, doc, "{}: rule add".format(writer))
    print("added rule {}#r{} (sync: {})".format(writer, n, res))
    return 0


def cmd_room_rule_rm(args: argparse.Namespace) -> int:
    ctx = Ctx()
    room, writer = require_config_writer(ctx, args.room)
    m = re.match(r"^([a-z0-9-]+)#r(\d+)$", args.ref)
    if not m:
        raise TeamError("rule reference must look like <writer>#r<n>, e.g. sanya-frontend#r2")
    full_key = "rule.{}.r{}".format(m.group(1), m.group(2))
    doc = load_config_doc(ctx, room, writer)
    writers_docs = room_config_writers(ctx, room)
    _set_field(doc, writers_docs, full_key, None)
    res = save_config_doc(ctx, room, writer, doc, "{}: rule rm".format(writer))
    print("removed rule {} (sync: {})".format(args.ref, res))
    return 0


def cmd_room_rule(args: argparse.Namespace) -> int:
    if args.rule_cmd == "add":
        return cmd_room_rule_add(args)
    if args.rule_cmd == "rm":
        return cmd_room_rule_rm(args)
    raise TeamError('usage: room rule add "<text>" | room rule rm <writer>#r<n>')


def cmd_room_export(args: argparse.Namespace) -> int:
    ctx = Ctx()
    room = resolve_show_room(ctx, args.room)
    eff = effective_config(ctx, room)
    active = {k: v for k, v in eff.items() if v["value"] is not None}
    tmpl: Dict[str, Any] = {}
    for simple in ("title", "description", "goal"):
        if simple in active:
            tmpl[simple] = active[simple]["value"]
    rules = [v["value"] for k, v in sorted(active.items()) if k.startswith("rule.")]
    if rules:
        tmpl["rules"] = rules
    policy = {k[len("policy."):]: v["value"] for k, v in active.items() if k.startswith("policy.")}
    if policy:
        tmpl["policy"] = policy
    fields = {k: v["value"] for k, v in active.items() if is_custom_key(k)}
    if fields:
        tmpl["fields"] = fields
    print(json.dumps(tmpl, indent=2, ensure_ascii=False))
    return 0


def cmd_room(args: argparse.Namespace) -> int:
    sub = getattr(args, "room_cmd", None)
    if sub == "create":
        return cmd_room_create(args)
    if sub == "set":
        return cmd_room_set(args)
    if sub == "unset":
        return cmd_room_unset(args)
    if sub == "rule":
        return cmd_room_rule(args)
    if sub == "export":
        return cmd_room_export(args)
    return cmd_room_show(args)


def cmd_self_update(args: argparse.Namespace) -> int:
    run_self_update()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ctx = Ctx()
    s = ctx.store
    age = s.last_sync_age()
    info = {
        "version": VERSION, "python": sys.version.split()[0], "repo": ctx.repo.root,
        "config_file": os.path.exists(os.path.join(ctx.repo.root, CONFIG_FILE)),
        "config": ctx.cfg, "identity": ctx.ident, "branch_tip": s.tip(),
        "remote_configured": s.remote_ok(), "remote_tip": s.tip(s.remote_ref) if s.remote_ok() else None,
        "last_sync_sec_ago": int(age) if age is not None else None,
        "agents_total": len(ctx.all_docs()),
        "error_log": os.path.join(ctx.repo.shared_dir, "error.log"),
        "push_error": push_error_info(ctx.repo),
        "self_update": self_update_doctor_info(),
    }
    print(json.dumps(info, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="team", description="team-sync: share status between Claude Code agents")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    def add(name: str, fn: Any, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help)
        sp.set_defaults(func=fn)
        return sp

    sp = add("init", cmd_init, "write .team-sync.json and create the team branch")
    sp.add_argument("--remote")
    sp.add_argument("--local", action="store_true", help="no remote: same machine only")
    sp.add_argument("--branch")
    sp.add_argument("--auto-observe", dest="auto_observe", metavar="ROOM",
                    help="every session in this repo observes ROOM ('' to disable)")

    sp = add("join", cmd_join, "join a room with this worktree's agent")
    sp.add_argument("room")
    sp.add_argument("--as", dest="as_", metavar="NAME", help="agent name (default: worktree dir name)")
    sp.add_argument("--role", choices=ROLES, default="member")
    sp.add_argument("--owner", help="human owner (default: from git user.email)")

    add("leave", cmd_leave, "leave the room (mark offline)")
    add("whoami", cmd_whoami, "show this agent's identity")

    sp = add("status", cmd_status, "show the room")
    sp.add_argument("--room")
    sp.add_argument("--all", action="store_true", help="all rooms")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--no-sync", action="store_true")

    sp = add("rooms", cmd_rooms, "list every room on the team branch")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--no-sync", action="store_true")

    sp = add("update", cmd_update, "update your status")
    sp.add_argument("--status", choices=STATUSES)
    sp.add_argument("--working-on", dest="working_on")
    sp.add_argument("--blocked-on", dest="blocked_on")
    sp.add_argument("--unblock", action="store_true")
    sp.add_argument("--handoff")

    for name, fn, h in (("learn", cmd_learn, "share a non-obvious learning"),
                        ("decide", cmd_decide, "record a team decision/convention")):
        sp = add(name, fn, h)
        sp.add_argument("text")
    sp = add("change", cmd_change, "announce a change others may depend on")
    sp.add_argument("text")
    sp.add_argument("--breaking", action="store_true")
    sp = add("announce", cmd_announce, "announce something ready for other agents to use")
    sp.add_argument("text")
    sp = add("ask", cmd_ask, "ask another agent (or 'all') for something")
    sp.add_argument("to")
    sp.add_argument("text")
    sp = add("reply", cmd_reply, "reply to an ask: <agent-id>#<ask-id>")
    sp.add_argument("ref")
    sp.add_argument("text")
    sp.add_argument("--done", action="store_true")
    sp = add("close", cmd_close, "close one of your own asks")
    sp.add_argument("ask_id")

    sp = add("claim", cmd_claim, "claim files/directories you are about to change")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--note")
    sp = add("release", cmd_release, "release claims")
    sp.add_argument("paths", nargs="*")
    sp.add_argument("--all", action="store_true")
    sp = add("check", cmd_check, "check paths for overlaps with other agents")
    sp.add_argument("paths", nargs="+")

    add("sync", cmd_sync, "fetch/merge/push the team branch now")

    sp = add("room", cmd_room, "room configuration: title, description, goal, rules, policy, fields")
    sp.add_argument("--room")
    sp.add_argument("--goal", help="legacy: set the room goal (kept for v0.1 compatibility)")
    sp.add_argument("--json", action="store_true")
    room_sub = sp.add_subparsers(dest="room_cmd")
    room_sub.required = False

    rs = room_sub.add_parser("show", help="show the effective room configuration")
    rs.add_argument("--room")
    rs.add_argument("--json", action="store_true")

    rc = room_sub.add_parser("create", help="create/configure a room (does not join)")
    rc.add_argument("room")
    rc.add_argument("--title")
    rc.add_argument("--description")
    rc.add_argument("--goal")
    rc.add_argument("--from", dest="from_file", metavar="FILE",
                    help="bootstrap from a template file (see 'room export')")

    rset = room_sub.add_parser("set", help="set a room config key")
    rset.add_argument("key")
    rset.add_argument("value")
    rset.add_argument("--room")

    runset = room_sub.add_parser("unset", help="unset a room config key")
    runset.add_argument("key")
    runset.add_argument("--room")

    rrule = room_sub.add_parser("rule", help="add or remove a room rule")
    rule_sub = rrule.add_subparsers(dest="rule_cmd")
    rule_sub.required = True
    ra = rule_sub.add_parser("add", help="add a room rule")
    ra.add_argument("text")
    ra.add_argument("--room")
    rr = rule_sub.add_parser("rm", help="remove a room rule")
    rr.add_argument("ref", metavar="<writer>#r<n>")
    rr.add_argument("--room")

    rexp = room_sub.add_parser("export", help="print the room config as a template")
    rexp.add_argument("--room")

    add("doctor", cmd_doctor, "diagnostics")
    add("self-update", cmd_self_update,
        "(internal, also usable by hand) check the marketplace and update team-sync in the background")

    sp = sub.add_parser("hook", help="(internal) Claude Code hook handler")
    sp.add_argument("event", choices=sorted(HOOKS))
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "hook":
        return hook_main(args.event)
    try:
        return int(args.func(args) or 0)
    except NotARepo:
        print("team-sync: not inside a git repository", file=sys.stderr)
        return 2
    except (TeamError, GitError) as e:
        print("team-sync: {}".format(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
