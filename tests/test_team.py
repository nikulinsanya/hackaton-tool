"""
End-to-end tests for team-sync.

Simulates two developer machines (two clones of a bare "origin"), one of them
running two agents in two worktrees. Run with:

    python3 -m unittest discover -s tests -v
"""
import atexit
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TEAM = os.path.join(HERE, "..", "plugins", "team-sync", "scripts", "team.py")

# Names baked into the per-process template (see _build_template): the origin
# plus the clones/worktree every test's setUp asks for. Env.clone()/
# Env.worktree() recognize these and skip straight to the copied directory
# instead of re-running git; any other name still goes through real git calls.
_TEMPLATE_CLONES = ("sanya", "anna")
_TEMPLATE_WORKTREES = {"sanya-tests-wt": "sanya"}

_TEMPLATE_LOCK = threading.Lock()
_TEMPLATE_DIR = None


def git(cwd, *args):
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True).stdout.strip()


def _ident(repo, name):
    git(repo, "config", "user.email", name + "@example.com")
    git(repo, "config", "user.name", name)


def _build_template():
    """Builds the origin + seed commit + standard clones/worktree once per
    process. Every Env() then copies this directory instead of repeating all
    of this git plumbing per test."""
    tmp = tempfile.mkdtemp(prefix="team-sync-template-")
    os.makedirs(os.path.join(tmp, "home"))
    origin = os.path.join(tmp, "origin.git")
    git(tmp, "init", "--bare", "-q", "--initial-branch=main", origin)
    seed = os.path.join(tmp, "seed")
    git(tmp, "init", "-q", "-b", "main", seed)
    _ident(seed, "seed")
    os.makedirs(os.path.join(seed, "src", "auth"))
    os.makedirs(os.path.join(seed, "src", "api"))
    for f in ("src/auth/login.ts", "src/auth/session.ts", "src/api/routes.ts", "README.md"):
        with open(os.path.join(seed, f), "w") as fh:
            fh.write("// " + f + "\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "init")
    git(seed, "remote", "add", "origin", origin)
    git(seed, "push", "-q", "origin", "main")
    shutil.rmtree(seed)
    for name in _TEMPLATE_CLONES:
        path = os.path.join(tmp, name)
        git(tmp, "clone", "-q", origin, path)
        _ident(path, name)
    for wt_name, repo_name in _TEMPLATE_WORKTREES.items():
        repo = os.path.join(tmp, repo_name)
        wt_path = os.path.join(tmp, wt_name)
        git(repo, "worktree", "add", "-q", "-b", wt_name, wt_path)
    return tmp


def _get_template():
    global _TEMPLATE_DIR
    with _TEMPLATE_LOCK:
        if _TEMPLATE_DIR is None:
            _TEMPLATE_DIR = _build_template()
            atexit.register(shutil.rmtree, _TEMPLATE_DIR, ignore_errors=True)
        return _TEMPLATE_DIR


class Env:
    """A sandbox with origin + clones + worktrees, copied from a per-process
    template so each test pays only a shutil.copytree plus a couple of path
    fixups instead of re-running git init/clone/push/worktree add."""

    def __init__(self):
        template = _get_template()
        self.tmp = tempfile.mkdtemp(prefix="team-sync-test-")
        shutil.rmtree(self.tmp)  # copytree wants to create the destination itself
        shutil.copytree(template, self.tmp)
        self.home = os.path.join(self.tmp, "home")
        self.origin = os.path.join(self.tmp, "origin.git")
        for name in _TEMPLATE_CLONES:
            git(os.path.join(self.tmp, name), "remote", "set-url", "origin", self.origin)
        for wt_name, repo_name in _TEMPLATE_WORKTREES.items():
            repo = os.path.join(self.tmp, repo_name)
            wt_path = os.path.join(self.tmp, wt_name)
            git(repo, "worktree", "repair", wt_path)

    def _ident(self, repo, name):
        _ident(repo, name)

    def clone(self, name):
        path = os.path.join(self.tmp, name)
        if name in _TEMPLATE_CLONES:
            return path  # already present (and remote-fixed) from the template
        git(self.tmp, "clone", "-q", self.origin, path)
        self._ident(path, name)
        return path

    def worktree(self, repo, name):
        path = os.path.join(self.tmp, name)
        if name in _TEMPLATE_WORKTREES:
            return path  # already present (and repaired) from the template
        git(repo, "worktree", "add", "-q", "-b", name, path)
        return path

    def env(self, **extra):
        e = dict(os.environ)
        for k in list(e):
            if k.startswith("CLAUDE_TEAM_") or k == "CLAUDE_PROJECT_DIR":
                del e[k]
        e["HOME"] = self.home
        e["GIT_CONFIG_NOSYSTEM"] = "1"
        e.update({k: v for k, v in extra.items() if v is not None})
        return e

    def team(self, cwd, *args, check=True, **env):
        p = subprocess.run([sys.executable, TEAM] + list(args), cwd=cwd, env=self.env(**env),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check and p.returncode != 0:
            raise AssertionError("team {} failed ({}):\n{}\n{}".format(args, p.returncode, p.stdout, p.stderr))
        return p

    def hook(self, cwd, event, payload, **env):
        payload = dict(payload)
        payload.setdefault("cwd", cwd)
        payload.setdefault("session_id", "s-" + os.path.basename(cwd))
        p = subprocess.run([sys.executable, TEAM, "hook", event], cwd=cwd, env=self.env(**env),
                           input=json.dumps(payload), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True)
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout) if p.stdout.strip() else None

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def ctx_text(out):
    return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


def push_error_path(repo):
    """Local path to <shared_dir>/push_error.json for a clone or worktree."""
    common = git(repo, "rev-parse", "--git-common-dir")
    if not os.path.isabs(common):
        common = os.path.join(repo, common)
    return os.path.join(os.path.realpath(common), "team-sync", "push_error.json")


def install_pre_receive(origin, message):
    """Installs an executable pre-receive hook in a bare repo that rejects every
    push with `message` on stderr, the way a GitLab project without write access
    would. Fetch is unaffected: pre-receive only runs on push."""
    hooks_dir = os.path.join(origin, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    path = os.path.join(hooks_dir, "pre-receive")
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\necho {} >&2\nexit 1\n".format(shlex.quote(message)))
    os.chmod(path, 0o755)


def remove_pre_receive(origin):
    try:
        os.remove(os.path.join(origin, "hooks", "pre-receive"))
    except OSError:
        pass


class TeamSyncTests(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.sanya = self.e.clone("sanya")                  # machine 1, main checkout
        self.sanya_tests = self.e.worktree(self.sanya, "sanya-tests-wt")  # machine 1, 2nd agent
        self.anna = self.e.clone("anna")                    # machine 2

    def tearDown(self):
        self.e.cleanup()

    def join_all(self):
        self.e.team(self.sanya, "join", "hackathon", "--as", "frontend")
        self.e.team(self.sanya_tests, "join", "hackathon", "--as", "tests")
        self.e.team(self.anna, "join", "hackathon", "--as", "api")

    def ids(self, cwd):
        out = self.e.team(cwd, "status", "--json").stdout
        return {d["id"]: d for d in json.loads(out)}

    # ---------------------------------------------------------------- basics

    def test_not_joined_is_noop(self):
        out = self.e.hook(self.sanya, "session-start", {})
        self.assertIsNone(out)
        out = self.e.hook(self.sanya, "pre-edit", {"tool_name": "Edit",
                                                  "tool_input": {"file_path": os.path.join(self.sanya, "README.md")}})
        self.assertIsNone(out)
        # and nothing was written
        self.assertEqual(git(self.sanya, "branch", "--list", "team-sync"), "")

    def test_join_and_status_across_worktrees_and_machines(self):
        self.join_all()
        self.e.team(self.sanya, "sync")
        ids = self.ids(self.sanya)
        self.assertEqual(set(ids), {"sanya-frontend", "sanya-tests", "anna-api"})
        ids = self.ids(self.anna)
        self.assertIn("sanya-frontend", ids)

    def test_same_machine_sees_updates_without_remote(self):
        e = self.e
        e.team(self.sanya, "join", "hackathon", "--as", "frontend", CLAUDE_TEAM_REMOTE="none")
        e.team(self.sanya_tests, "join", "hackathon", "--as", "tests", CLAUDE_TEAM_REMOTE="none")
        e.team(self.sanya, "update", "--status", "in_progress", "--working-on", "login form",
               CLAUDE_TEAM_REMOTE="none")
        out = e.team(self.sanya_tests, "status", "--json", CLAUDE_TEAM_REMOTE="none").stdout
        docs = {d["id"]: d for d in json.loads(out)}
        self.assertEqual(docs["sanya-frontend"]["working_on"], "login form")
        # nothing pushed
        self.assertEqual(git(self.e.origin, "branch", "--list", "team-sync"), "")

    def test_env_identity(self):
        out = self.e.team(self.anna, "whoami", CLAUDE_TEAM_ROOM="hackathon", CLAUDE_TEAM_AGENT="db").stdout
        ident = json.loads(out)
        self.assertEqual(ident["id"], "anna-db")
        self.assertEqual(ident["source"], "env")

    # ---------------------------------------------------------- concurrency

    def test_divergent_histories_merge(self):
        self.join_all()
        # both machines write while "offline" from each other
        self.e.team(self.sanya, "update", "--working-on", "A", CLAUDE_TEAM_REMOTE="none")
        self.e.team(self.anna, "update", "--working-on", "B", CLAUDE_TEAM_REMOTE="none")
        self.e.team(self.sanya, "sync")
        self.e.team(self.anna, "sync")
        self.e.team(self.sanya, "sync")
        for cwd in (self.sanya, self.anna):
            ids = self.ids(cwd)
            self.assertEqual(ids["sanya-frontend"]["working_on"], "A")
            self.assertEqual(ids["anna-api"]["working_on"], "B")
            # an ordinary race between two clones is not a reported push failure
            self.assertFalse(os.path.exists(push_error_path(cwd)))

    def test_parallel_writers_same_machine(self):
        e = self.e
        e.team(self.sanya, "join", "r", "--as", "a", CLAUDE_TEAM_REMOTE="none")
        e.team(self.sanya_tests, "join", "r", "--as", "b", CLAUDE_TEAM_REMOTE="none")
        procs = []
        for i in range(6):
            for cwd in (self.sanya, self.sanya_tests):
                procs.append(subprocess.Popen(
                    [sys.executable, TEAM, "learn", "note {} from {}".format(i, os.path.basename(cwd))],
                    cwd=cwd, env=e.env(CLAUDE_TEAM_REMOTE="none"),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for p in procs:
            p.wait()
            self.assertEqual(p.returncode, 0, p.stderr.read())
        docs = {d["id"]: d for d in json.loads(
            e.team(self.sanya, "status", "--json", CLAUDE_TEAM_REMOTE="none").stdout)}
        # a doc is rewritten whole by its owner, so concurrent writes by the SAME agent may
        # lose items; writes by different agents must never clobber each other.
        self.assertGreaterEqual(len(docs["sanya-a"]["learnings"]), 1)
        self.assertGreaterEqual(len(docs["sanya-b"]["learnings"]), 1)

    # ---------------------------------------------------------- push errors

    def test_push_failure_reported_then_cleared(self):
        install_pre_receive(self.e.origin, "GitLab: You are not allowed to push code to this project.")
        self.e.team(self.sanya, "join", "hackathon", "--as", "frontend")
        self.e.team(self.sanya, "update", "--working-on", "A")

        # local branch has the update, origin does not
        local = self.ids(self.sanya)["sanya-frontend"]
        self.assertEqual(local["working_on"], "A")
        p = subprocess.run(["git", "show", "team-sync:rooms/hackathon/agents/sanya-frontend.json"],
                           cwd=self.e.origin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(p.returncode, 0)

        perr_path = push_error_path(self.sanya)
        self.assertTrue(os.path.exists(perr_path))
        with open(perr_path) as f:
            perr = json.load(f)
        self.assertEqual(perr["remote"], "origin")
        self.assertIn("write access", perr["reason"])

        # team status shows it
        out = self.e.team(self.sanya, "status").stdout
        self.assertIn("not reaching the team", out)

        # the briefing of a new session shows it
        text = ctx_text(self.e.hook(self.sanya, "session-start", {}))
        self.assertIn("not reaching the team", text)

        # prompt digest: once, then quiet
        out1 = self.e.hook(self.sanya, "prompt", {"prompt": "hi"})
        self.assertIn("not reaching the team", ctx_text(out1))
        out2 = self.e.hook(self.sanya, "prompt", {"prompt": "hi again"})
        self.assertNotIn("not reaching the team", ctx_text(out2) or "")

        # fix the remote: next sync pushes the stored update and clears the error
        remove_pre_receive(self.e.origin)
        self.e.team(self.sanya, "sync")
        p = subprocess.run(["git", "show", "team-sync:rooms/hackathon/agents/sanya-frontend.json"],
                           cwd=self.e.origin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["working_on"], "A")
        self.assertFalse(os.path.exists(perr_path))

        out = self.e.team(self.sanya, "status").stdout
        self.assertNotIn("not reaching the team", out)

    def test_push_failure_silent_when_not_joined(self):
        perr_path = push_error_path(self.sanya)
        os.makedirs(os.path.dirname(perr_path), exist_ok=True)
        with open(perr_path, "w") as f:
            json.dump({"at": "2024-01-01T00:00:00Z", "remote": "origin", "reason": "no write access to origin"}, f)
        self.assertIsNone(self.e.hook(self.sanya, "session-start", {}))
        self.assertIsNone(self.e.hook(self.sanya, "prompt", {"prompt": "hi"}))

    # -------------------------------------------------------------- overlap

    def test_overlap_ask_once_then_quiet(self):
        with open(os.path.join(self.sanya, ".team-sync.json"), "w") as f:
            json.dump({"overlap_action": "ask"}, f)
        self.join_all()
        login = "src/auth/login.ts"
        self.e.hook(self.anna, "post-tool", {"tool_name": "Edit",
                                            "tool_input": {"file_path": os.path.join(self.anna, login)}})
        self.e.team(self.anna, "sync")
        self.e.team(self.sanya, "sync")
        self.e.hook(self.sanya, "session-start", {})
        payload = {"tool_name": "Edit",
                   "tool_input": {"file_path": os.path.join(self.sanya, "src/auth/session.ts")}}
        out = self.e.hook(self.sanya, "pre-edit", payload)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "ask")
        self.assertIn("anna-api", hso["permissionDecisionReason"])
        self.assertIsNone(self.e.hook(self.sanya, "pre-edit", payload))  # asked once
        # unrelated dir: no overlap
        payload["tool_input"]["file_path"] = os.path.join(self.sanya, "src/api/routes.ts")
        self.assertIsNone(self.e.hook(self.sanya, "pre-edit", payload))

    def test_default_overlap_is_warn(self):
        # no .team-sync.json, no room policy: overlap_action defaults to warn
        self.join_all()
        self.e.team(self.anna, "claim", "src/api")
        self.e.team(self.sanya, "sync")
        text = ctx_text(self.e.hook(self.sanya, "session-start", {}))
        self.assertIn("on an overlap warning, tell your human in one line", text)
        out = self.e.hook(self.sanya, "pre-edit", {"tool_name": "Edit", "tool_input": {
            "file_path": os.path.join(self.sanya, "src/api/routes.ts")}})
        hso = out["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hso)  # not held
        self.assertIn("anna-api", hso["additionalContext"])

    def test_claim_and_deny_mode(self):
        self.join_all()
        self.e.team(self.anna, "claim", "src/api")
        self.e.team(self.sanya, "sync")
        cfg = os.path.join(self.sanya, ".team-sync.json")
        with open(cfg, "w") as f:
            json.dump({"overlap_action": "deny"}, f)
        out = self.e.hook(self.sanya, "pre-edit", {"tool_name": "Write", "tool_input": {
            "file_path": os.path.join(self.sanya, "src/api/new/thing.ts")}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        p = self.e.team(self.sanya, "check", "src/api/routes.ts", check=False)
        self.assertEqual(p.returncode, 1)
        self.e.team(self.anna, "release", "--all")
        self.e.team(self.sanya, "sync")
        self.assertEqual(self.e.team(self.sanya, "check", "src/api/routes.ts").returncode, 0)

    def test_offline_and_stale_agents_are_ignored(self):
        self.join_all()
        self.e.team(self.anna, "claim", "src/api/")
        self.e.hook(self.anna, "session-end", {})
        self.e.team(self.sanya, "sync")
        self.assertEqual(self.e.team(self.sanya, "check", "src/api/x.ts").returncode, 0)
        # stale: anna back online, but time moves 2h forward
        self.e.team(self.anna, "update", "--status", "in_progress")
        self.e.team(self.sanya, "sync")
        self.assertEqual(self.e.team(self.sanya, "check", "src/api/x.ts", check=False).returncode, 1)
        self.assertEqual(self.e.team(self.sanya, "check", "src/api/x.ts",
                                     TEAM_SYNC_NOW="2099-01-01T00:00:00Z").returncode, 0)

    # --------------------------------------------------------------- digest

    def test_digest_ask_reply_breaking(self):
        self.join_all()
        self.e.hook(self.sanya, "session-start", {})
        self.e.hook(self.anna, "session-start", {})
        self.e.team(self.anna, "ask", "sanya-frontend", "Please export the zod schema")
        self.e.team(self.anna, "change", "getUser() returns null instead of throwing", "--breaking")
        self.e.team(self.anna, "decide", "Validation uses zod")
        self.e.team(self.sanya, "sync")
        text = ctx_text(self.e.hook(self.sanya, "prompt", {"prompt": "hi"}))
        self.assertIn("ASK for you from anna-api [anna-api#a1]", text)
        self.assertIn("BREAKING", text)
        self.assertIn("DECISION", text)
        self.assertIn("impact check", text)
        # second prompt: nothing new
        self.assertIsNone(self.e.hook(self.sanya, "prompt", {"prompt": "again"}))
        self.e.team(self.sanya, "reply", "anna-api#a1", "exported from src/schema.ts", "--done")
        self.e.team(self.anna, "sync")
        text = ctx_text(self.e.hook(self.anna, "prompt", {"prompt": "x"}))
        self.assertIn("sanya-frontend replied to your ask anna-api#a1", text)

    def test_session_start_briefing(self):
        self.join_all()
        self.e.team(self.anna, "update", "--status", "in_progress", "--working-on", "POST /session")
        self.e.team(self.anna, "ask", "sanya-frontend", "Need the login form to call /session")
        self.e.team(self.sanya, "room", "--goal", "Ship team-sync v1")
        text = ctx_text(self.e.hook(self.sanya, "session-start", {"source": "startup"}))
        self.assertIn('You are agent "sanya-frontend"', text)
        self.assertIn("Room goal: Ship team-sync v1", text)
        self.assertIn("POST /session", text)
        self.assertIn("Open ASK for you from anna-api", text)
        self.assertIn('tell your human with a proposed answer; after they agree: reply', text)
        self.assertNotIn("-> answer with: reply", text)

    def test_briefing_cheat_sheet_and_reaction_rules_match_protocol(self):
        # no policy set: overlap_action defaults to "warn"
        self.join_all()
        text = ctx_text(self.e.hook(self.sanya, "session-start", {"source": "startup"}))
        self.assertIn('propose to your human first, then reply', text)
        self.assertIn("Run each team command on its own (no &&, ;, pipes or redirects) "
                      "so it needs no permission prompt; avoid $ and backticks in the text.", text)
        self.assertIn("for every breaking change, start your answer with a one-line warning "
                      "plus a quick impact check, and propose (don't apply) the fix", text)
        self.assertIn("propose answers to asks addressed to you, reply after your human agrees", text)
        self.assertIn("on an overlap warning, tell your human in one line and keep going with a "
                      "minimal change (stop and ask only before refactoring that area)", text)

    def test_briefing_reaction_rules_ask_mode(self):
        # the overlap_rule branch in briefing() reads the local .team-sync.json
        # value directly (like the existing warn-mode test above), not room policy
        with open(os.path.join(self.sanya, ".team-sync.json"), "w") as f:
            json.dump({"overlap_action": "ask"}, f)
        self.join_all()
        text = ctx_text(self.e.hook(self.sanya, "session-start", {"source": "startup"}))
        self.assertIn("on an overlap, stop and ask your human", text)
        self.assertIn("for every breaking change, start your answer with a one-line warning "
                      "plus a quick impact check, and propose (don't apply) the fix", text)

    def test_joined_midsession_gets_briefing_on_prompt(self):
        self.e.team(self.sanya, "join", "hackathon", "--as", "frontend")
        text = ctx_text(self.e.hook(self.sanya, "prompt", {"session_id": "fresh"}))
        self.assertIn("You are agent", text)

    # ------------------------------------------------------------- observer

    def test_auto_observe(self):
        self.e.team(self.anna, "join", "hackathon", "--as", "api")
        self.e.team(self.anna, "claim", "src/auth/")
        with open(os.path.join(self.sanya, ".team-sync.json"), "w") as f:
            json.dump({"auto_observe": "hackathon", "overlap_action": "ask"}, f)
        self.e.team(self.sanya, "sync")
        text = ctx_text(self.e.hook(self.sanya, "session-start", {}))
        self.assertIn("as observer", text)
        out = self.e.hook(self.sanya, "pre-edit", {"tool_name": "Edit", "tool_input": {
            "file_path": os.path.join(self.sanya, "src/auth/login.ts")}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        # observers record touches so members are warned about them too
        self.e.hook(self.sanya, "post-tool", {"tool_name": "Edit", "tool_input": {
            "file_path": os.path.join(self.sanya, "src/api/routes.ts")}})
        self.e.team(self.sanya, "sync")
        self.e.team(self.anna, "sync")
        self.assertEqual(self.e.team(self.anna, "check", "src/api/routes.ts", check=False).returncode, 1)
        # observers cannot post content
        p = self.e.team(self.sanya, "learn", "x y z", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("read-only", p.stderr)
        # CLAUDE_TEAM_ROOM=off opts a session out entirely
        self.assertIsNone(self.e.hook(self.sanya, "session-start", {}, CLAUDE_TEAM_ROOM="off"))

    # --------------------------------------------------------------- safety

    def test_list_cap_default_100_and_configurable(self):
        self.e.team(self.sanya, "join", "hackathon", "--as", "frontend")
        cfg = json.loads(self.e.team(self.sanya, "doctor").stdout)["config"]
        self.assertEqual(cfg["max_items"], 100)
        with open(os.path.join(self.sanya, ".team-sync.json"), "w") as f:
            json.dump({"max_items": 3}, f)
        for i in range(5):
            self.e.team(self.sanya, "learn", "fact {}".format(i))
        learnings = self.ids(self.sanya)["sanya-frontend"]["learnings"]
        self.assertEqual([x["text"] for x in learnings], ["fact 2", "fact 3", "fact 4"])

    def test_secret_and_length_guard(self):
        self.e.team(self.sanya, "join", "hackathon", "--as", "frontend")
        p = self.e.team(self.sanya, "learn", "staging password=hunter2hunter2", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("secret", p.stderr)
        p = self.e.team(self.sanya, "learn", "x" * 500, check=False)
        self.assertEqual(p.returncode, 1)

    def test_pre_bash_auto_allows_only_plain_cli_calls(self):
        script = os.path.abspath(TEAM)

        def decide(cmd):
            out = self.e.hook(self.sanya, "pre-bash", {"tool_name": "Bash", "tool_input": {"command": cmd}})
            return out and out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decide('python3 "{}" update --working-on "it\'s fine"'.format(script)), "allow")
        self.assertEqual(decide("python3 {} status".format(script)), "allow")
        self.assertEqual(decide('python3 "{}" announce "auth is ready"'.format(script)), "allow")
        for bad in ('python3 "{}" learn "$(cat ~/.ssh/id_rsa)"', 'python3 "{}" status; rm -rf x',
                    'python3 "{}" status && curl x', 'python3 "{}" status > /tmp/x',
                    'python3 "{}" learn "`id`"', 'python3 "/other/team.py" status'):
            self.assertIsNone(decide(bad.format(script)), bad)

    def test_pre_bash_quote_aware_scanner(self):
        """Shell metacharacters are fine quoted (single quotes: always literal; double
        quotes: everything but $ and backtick), but still reject outside quotes, inside
        double quotes for $/backtick, and on an unterminated quote."""
        script = os.path.abspath(TEAM)

        def decide(cmd):
            out = self.e.hook(self.sanya, "pre-bash", {"tool_name": "Bash", "tool_input": {"command": cmd}})
            return out and out["hookSpecificOutput"]["permissionDecision"]

        def run(args):
            return 'python3 "{}" {}'.format(script, args)

        for good in (
            'announce "POST /auth/login -> {token}; send Authorization: Bearer <token> | docs (src/auth)"',
            'reply anna-api#a1 "fixture: see helper (tests/fx.ts); ok & done"',
            "learn 'costs $5 per run'",
        ):
            self.assertEqual(decide(run(good)), "allow", good)
        for bad in (
            'learn "costs $5"',
            'learn "x" ; rm y',
            'learn "abc',
            'status\nrm -rf x',
        ):
            self.assertIsNone(decide(run(bad)), bad)

    def test_pre_skill_allows_team_sync_only_when_joined(self):
        def decide(skill_input):
            out = self.e.hook(self.sanya, "pre-skill", {"tool_name": "Skill", "tool_input": skill_input})
            return out and out["hookSpecificOutput"]["permissionDecision"]

        # not joined: no decision either way
        self.assertIsNone(decide({"skill": "team-sync:team-protocol"}))

        self.e.team(self.sanya, "join", "hackathon", "--as", "frontend")
        self.assertEqual(decide({"skill": "team-sync:team-protocol"}), "allow")
        self.assertEqual(decide({"name": "team-sync:announce"}), "allow")  # defensive key
        self.assertEqual(decide({"command": "team-sync:handoff"}), "allow")  # defensive key
        self.assertIsNone(decide({"skill": "other-plugin:some-skill"}))

    def test_hooks_never_fail_outside_repo(self):
        out = self.e.hook(self.e.home, "session-start", {"cwd": self.e.home})
        self.assertIsNone(out)

    def test_main_branch_untouched(self):
        self.join_all()
        self.e.team(self.sanya, "learn", "something useful")
        self.assertEqual(git(self.sanya, "status", "--porcelain"), "")
        self.assertEqual(git(self.sanya, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        files = git(self.e.origin, "ls-tree", "-r", "--name-only", "team-sync").split("\n")
        self.assertIn("rooms/hackathon/agents/sanya-frontend.json", files)

    # ---------------------------------------------------------- room config

    def _tmpl_path(self, name, data):
        path = os.path.join(self.e.tmp, name)
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def test_room_create_set_unset_rule_and_export(self):
        self.e.team(self.sanya, "join", "hackathon", "--as", "frontend")
        self.e.team(self.sanya, "room", "create", "hackathon", "--title", "Hack Day",
                   "--description", "Build stuff", "--goal", "Ship it")
        out = self.e.team(self.sanya, "room", "show", "--json").stdout
        fields = json.loads(out)["fields"]
        self.assertEqual(fields["title"]["value"], "Hack Day")
        self.assertEqual(fields["description"]["value"], "Build stuff")
        self.assertEqual(fields["goal"]["value"], "Ship it")

        self.e.team(self.sanya, "room", "set", "track", "dashboard")
        self.e.team(self.sanya, "room", "rule", "add", "Use zod for validation")
        fields = json.loads(self.e.team(self.sanya, "room", "show", "--json").stdout)["fields"]
        self.assertEqual(fields["track"]["value"], "dashboard")
        rule_keys = [k for k in fields if k.startswith("rule.")]
        self.assertEqual(len(rule_keys), 1)
        self.assertEqual(fields[rule_keys[0]]["value"], "Use zod for validation")
        self.assertTrue(rule_keys[0].startswith("rule.sanya-frontend."))

        self.e.team(self.sanya, "room", "unset", "track")
        fields = json.loads(self.e.team(self.sanya, "room", "show", "--json").stdout)["fields"]
        self.assertNotIn("track", fields)

        self.e.team(self.sanya, "room", "rule", "rm", "sanya-frontend#r1")
        fields = json.loads(self.e.team(self.sanya, "room", "show", "--json").stdout)["fields"]
        self.assertFalse([k for k in fields if k.startswith("rule.")])

        # room.json still carries the mirrored goal for v0.1 clients
        self.e.team(self.sanya, "room", "--goal", "Ship it")
        room_doc = json.loads(git(self.e.origin, "show", "team-sync:rooms/hackathon/room.json"))
        self.assertEqual(room_doc["goal"], "Ship it")

        # status --json lists only agents; config files never show up there
        docs = json.loads(self.e.team(self.sanya, "status", "--json").stdout)
        self.assertEqual({d["id"] for d in docs}, {"sanya-frontend"})
        for d in docs:
            self.assertNotIn("fields", d)

    def test_room_from_template_round_trip_and_flag_override(self):
        tmpl = self._tmpl_path("tmpl.json", {
            "title": "Hack Day", "description": "Build stuff", "goal": "Ship it",
            "rules": ["Use zod for validation"],
            "policy": {"overlap_action": "warn"},
            "fields": {"track": "dashboard"},
        })
        self.e.team(self.sanya, "room", "create", "hackathon", "--from", tmpl)
        out1 = self.e.team(self.sanya, "room", "export", "--room", "hackathon").stdout
        self.assertEqual(json.loads(out1), json.load(open(tmpl)))

        # a flag passed alongside --from overrides the template's value for that key
        tmpl2 = self._tmpl_path("tmpl2.json", {"title": "From template", "goal": "G"})
        self.e.team(self.sanya, "room", "create", "r2", "--from", tmpl2, "--title", "From flag")
        fields = json.loads(self.e.team(self.sanya, "room", "show", "--room", "r2",
                                       "--json").stdout)["fields"]
        self.assertEqual(fields["title"]["value"], "From flag")
        self.assertEqual(fields["goal"]["value"], "G")

    def test_room_validation_errors(self):
        self.e.team(self.sanya, "join", "hackathon", "--as", "frontend")

        def fails(*args, needle=None):
            p = self.e.team(self.sanya, *args, check=False)
            self.assertEqual(p.returncode, 1, args)
            if needle:
                self.assertIn(needle, p.stderr)

        fails("room", "set", "Bad Key", "x", needle="bad field name")
        fails("room", "set", "branch", "x", needle="cannot be a room field")
        fails("room", "set", "overlap_action", "warn", needle="use policy.overlap_action")
        fails("room", "set", "policy.nope", "x", needle="not a room policy")
        fails("room", "set", "policy.overlap_action", "bogus", needle="ask|warn|deny")
        fails("room", "set", "policy.breaking_check", "bogus", needle="true/false")
        fails("room", "set", "note", "password=hunter2hunter2", needle="secret")
        fails("room", "set", "description", "x" * 1001, needle="too long")
        self.e.team(self.sanya, "room", "set", "description", "x" * 1000)  # exactly at the limit
        self.e.team(self.sanya, "room", "set", "policy.breaking_check", "false")

    def test_room_custom_field_and_rule_limits(self):
        # seed 29 of each through one template write, so the boundary itself is the
        # only part exercised one CLI call at a time (30 individual writes would be slow)
        tmpl = self._tmpl_path("limits.json", {
            "fields": {"field{}".format(i): "v" for i in range(29)},
            "rules": ["rule {}".format(i) for i in range(29)],
        })
        self.e.team(self.sanya, "room", "create", "hackathon", "--from", tmpl)
        self.e.team(self.sanya, "join", "hackathon", "--as", "frontend")
        self.e.team(self.sanya, "room", "set", "field29", "v")  # 30th: still ok
        p = self.e.team(self.sanya, "room", "set", "field30", "v", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("30 active custom fields", p.stderr)
        self.e.team(self.sanya, "room", "rule", "add", "rule 29")  # 30th: still ok
        p = self.e.team(self.sanya, "room", "rule", "add", "one too many", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("30 active rules", p.stderr)

    def test_room_permissions_observer_and_non_joined(self):
        self.e.team(self.anna, "join", "hackathon", "--as", "api")
        self.e.team(self.anna, "room", "create", "hackathon", "--title", "T")
        self.e.team(self.anna, "sync")
        # observer refused
        self.e.team(self.sanya, "join", "hackathon", "--as", "obs", "--role", "observer")
        p = self.e.team(self.sanya, "room", "set", "title", "nope", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("observers are read-only", p.stderr)
        self.e.team(self.sanya, "leave")
        # non-joined human: allowed with --room
        self.e.team(self.sanya, "sync")
        self.e.team(self.sanya, "room", "set", "title", "from human", "--room", "hackathon")
        fields = json.loads(self.e.team(self.sanya, "room", "show", "--room", "hackathon",
                                       "--json").stdout)["fields"]
        self.assertEqual(fields["title"]["value"], "from human")
        # non-joined human: refused without --room
        p = self.e.team(self.sanya, "room", "set", "title", "x", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("not joined", p.stderr)

    def test_room_config_merge_semantics(self):
        self.join_all()
        # two machines offline, editing different keys: both survive after sync
        self.e.team(self.sanya, "room", "set", "title", "Frontend's title", CLAUDE_TEAM_REMOTE="none")
        self.e.team(self.anna, "room", "set", "description", "Api's description", CLAUDE_TEAM_REMOTE="none")
        self.e.team(self.sanya, "sync")
        self.e.team(self.anna, "sync")
        self.e.team(self.sanya, "sync")
        for cwd in (self.sanya, self.anna):
            fields = json.loads(self.e.team(cwd, "room", "show", "--json").stdout)["fields"]
            self.assertEqual(fields["title"]["value"], "Frontend's title")
            self.assertEqual(fields["description"]["value"], "Api's description")

        # two machines offline, editing the SAME key: both clones agree on one winner
        self.e.team(self.sanya, "room", "set", "track", "frontend-wins", CLAUDE_TEAM_REMOTE="none")
        self.e.team(self.anna, "room", "set", "track", "api-wins", CLAUDE_TEAM_REMOTE="none")
        self.e.team(self.sanya, "sync")
        self.e.team(self.anna, "sync")
        self.e.team(self.sanya, "sync")
        winners = {json.loads(self.e.team(cwd, "room", "show", "--json").stdout)["fields"]
                  ["track"]["value"] for cwd in (self.sanya, self.anna)}
        self.assertEqual(len(winners), 1, "both clones must agree on the same winner")

        # a causal edit (made after seeing the previous one) wins even when its
        # writer's wall clock is far in the past: the Lamport clock, not time, orders it
        self.e.team(self.sanya, "room", "set", "track2", "first")
        self.e.team(self.anna, "sync")
        self.e.team(self.anna, "room", "set", "track2", "second-causal",
                   TEAM_SYNC_NOW="2000-01-01T00:00:00Z")
        self.e.team(self.anna, "sync")
        self.e.team(self.sanya, "sync")
        for cwd in (self.sanya, self.anna):
            fields = json.loads(self.e.team(cwd, "room", "show", "--json").stdout)["fields"]
            self.assertEqual(fields["track2"]["value"], "second-causal")

    def test_room_policy_precedence_and_pre_edit_deny(self):
        with open(os.path.join(self.sanya, ".team-sync.json"), "w") as f:
            json.dump({"overlap_action": "ask"}, f)
        self.join_all()
        self.e.team(self.anna, "claim", "src/api")
        self.e.team(self.anna, "room", "set", "policy.overlap_action", "deny")
        self.e.team(self.anna, "sync")
        self.e.team(self.sanya, "sync")
        # room policy (deny) overrides the local .team-sync.json (ask)
        out = self.e.hook(self.sanya, "pre-edit", {"tool_name": "Write", "tool_input": {
            "file_path": os.path.join(self.sanya, "src/api/new/thing.ts")}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        # unsetting the room policy restores the local .team-sync.json value (ask)
        self.e.team(self.anna, "room", "unset", "policy.overlap_action")
        self.e.team(self.sanya, "sync")
        out = self.e.hook(self.sanya, "pre-edit", {"tool_name": "Write", "tool_input": {
            "file_path": os.path.join(self.sanya, "src/api/new/thing2.ts")}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_room_policy_warn_lets_edit_through_and_softens_briefing(self):
        self.join_all()
        self.e.team(self.anna, "claim", "src/api")
        self.e.team(self.anna, "room", "set", "policy.overlap_action", "warn")
        self.e.team(self.sanya, "sync")
        text = ctx_text(self.e.hook(self.sanya, "session-start", {}))
        self.assertIn("on an overlap warning, tell your human in one line", text)
        out = self.e.hook(self.sanya, "pre-edit", {"tool_name": "Edit", "tool_input": {
            "file_path": os.path.join(self.sanya, "src/api/routes.ts")}})
        hso = out["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hso)  # not held
        self.assertIn("anna-api", hso["additionalContext"])

    def test_pre_bash_asks_for_policy_and_from(self):
        script = os.path.abspath(TEAM)

        def decide(cmd):
            out = self.e.hook(self.sanya, "pre-bash", {"tool_name": "Bash", "tool_input": {"command": cmd}})
            return out and out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decide('python3 "{}" room set policy.overlap_action warn'.format(script)), "ask")
        self.assertEqual(decide('python3 "{}" room unset policy.overlap_action'.format(script)), "ask")
        self.assertEqual(decide('python3 "{}" room create x --from f.json'.format(script)), "ask")  # missing file
        no_policy = self._tmpl_path("nopolicy.json", {"title": "T"})
        self.assertEqual(decide('python3 "{}" room create x --from {}'.format(script, no_policy)), "allow")
        with_policy = self._tmpl_path("withpolicy.json", {"title": "T", "policy": {"overlap_action": "warn"}})
        self.assertEqual(decide('python3 "{}" room create x --from {}'.format(script, with_policy)), "ask")
        self.assertEqual(decide('python3 "{}" room set title X'.format(script)), "allow")
        self.assertEqual(decide('python3 "{}" room rule add "hi"'.format(script)), "allow")

    def test_room_create_invalid_template_writes_nothing(self):
        self.e.team(self.sanya, "init")
        before = git(self.sanya, "rev-parse", "team-sync")
        tmpl = self._tmpl_path("bad.json", {"title": "x", "policy": {"overlap_action": "bogus"}})
        p = self.e.team(self.sanya, "room", "create", "brandnew", "--from", tmpl, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("ask|warn|deny", p.stderr)
        self.assertEqual(git(self.sanya, "rev-parse", "team-sync"), before)
        self.assertEqual(git(self.e.origin, "rev-parse", "team-sync"), before)

    # ---------------------------------------------------------------- rooms

    def test_rooms_empty_branch(self):
        self.e.team(self.sanya, "init")
        out = self.e.team(self.sanya, "rooms").stdout
        self.assertIn("No rooms yet. A lead creates one with /team-sync:start <name>.", out)
        self.assertEqual(json.loads(self.e.team(self.sanya, "rooms", "--json").stdout), [])

    def test_rooms_lists_config_only_and_agent_rooms_not_joined(self):
        # a room created by `room create` with nobody ever joined still shows up
        self.e.team(self.sanya, "room", "create", "config-only", "--title", "Config Only",
                   "--goal", "Just config")
        self.e.team(self.sanya, "join", "hackathon", "--as", "frontend")
        self.e.team(self.anna, "join", "hackathon", "--as", "api")
        self.e.team(self.anna, "sync")
        self.e.team(self.sanya, "sync")
        # not joined: still works
        out = self.e.team(self.anna, "leave").stdout
        self.assertIn("offline", out)
        rooms = {r["room"]: r for r in json.loads(self.e.team(self.anna, "rooms", "--json").stdout)}
        self.assertEqual(set(rooms), {"config-only", "hackathon"})
        self.assertEqual(rooms["config-only"]["title"], "Config Only")
        self.assertEqual(rooms["config-only"]["goal"], "Just config")
        self.assertEqual(rooms["config-only"]["online"], [])
        self.assertEqual(rooms["config-only"]["agents"], 0)
        # anna just left (offline), sanya-frontend still online
        self.assertEqual(rooms["hackathon"]["online"], ["sanya-frontend"])
        self.assertEqual(rooms["hackathon"]["agents"], 2)

    def test_rooms_newest_first(self):
        self.e.team(self.sanya, "room", "create", "old-room", "--title", "Old")
        self.e.team(self.sanya, "room", "create", "new-room", "--title", "New",
                   TEAM_SYNC_NOW="2099-01-01T00:00:00Z")
        rooms = [r["room"] for r in json.loads(self.e.team(self.sanya, "rooms", "--json").stdout)]
        self.assertEqual(rooms, ["new-room", "old-room"])

    def test_rooms_plain_output_format(self):
        self.e.team(self.sanya, "room", "create", "idea-s", "--title", "Idea S",
                   "--goal", "Login page")
        self.e.team(self.sanya, "join", "idea-s", "--as", "frontend")
        out = self.e.team(self.sanya, "rooms").stdout.strip()
        self.assertEqual(out, 'idea-s  "Idea S"  goal: Login page  · 1 online (sanya-frontend), 1 total')

    def test_rooms_pre_bash_auto_allowed(self):
        script = os.path.abspath(TEAM)
        out = self.e.hook(self.sanya, "pre-bash", {"tool_name": "Bash",
                                                   "tool_input": {"command": 'python3 "{}" rooms'.format(script)}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "allow")

    # --------------------------------------------------------------- announce

    def test_announce_stores_ready_item(self):
        self.join_all()
        self.e.team(self.anna, "announce", "Authorization is ready: POST /auth/login")
        self.e.team(self.anna, "sync")
        doc = json.loads(git(self.e.origin, "show", "team-sync:rooms/hackathon/agents/anna-api.json"))
        ready = [c for c in doc["changes"] if c.get("kind") == "ready"]
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["breaking"], False)
        self.assertEqual(ready[0]["text"], "Authorization is ready: POST /auth/login")

    def test_announce_observer_refused(self):
        self.e.team(self.sanya, "join", "hackathon", "--as", "obs", "--role", "observer")
        p = self.e.team(self.sanya, "announce", "x ready", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("read-only", p.stderr)

    def test_announce_secret_and_length_guard(self):
        self.e.team(self.sanya, "join", "hackathon", "--as", "frontend")
        p = self.e.team(self.sanya, "announce", "use token=hunter2hunter2", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("secret", p.stderr)
        p = self.e.team(self.sanya, "announce", "x" * 500, check=False)
        self.assertEqual(p.returncode, 1)

    def test_announce_digest_shows_ready_not_breaking(self):
        self.join_all()
        self.e.hook(self.sanya, "session-start", {})
        self.e.hook(self.anna, "session-start", {})
        self.e.team(self.anna, "announce", "Authorization is ready: POST /auth/login")
        self.e.team(self.sanya, "sync")
        text = ctx_text(self.e.hook(self.sanya, "prompt", {"prompt": "hi"}))
        self.assertIn("* READY from anna-api: Authorization is ready", text)
        self.assertNotIn("!! BREAKING", text)
        self.assertIn("tell your human in one line at the start of your answer", text)
        self.assertIn("git pull --rebase --autostash", text)  # pull main before using it

    def test_announce_briefing_lists_recent_announcements_even_offline(self):
        self.join_all()
        self.e.team(self.anna, "announce", "Authorization is ready: POST /auth/login")
        self.e.hook(self.anna, "session-end", {})
        self.e.team(self.sanya, "sync")
        text = ctx_text(self.e.hook(self.sanya, "session-start", {}))
        self.assertIn("Available from the team (announced):", text)
        self.assertIn("Authorization is ready: POST /auth/login", text)
        self.assertIn("anna-api", text)

    def test_announce_older_than_7_days_not_in_briefing(self):
        old = {"TEAM_SYNC_NOW": "2020-01-01T00:00:00Z"}
        self.e.team(self.anna, "join", "hackathon", "--as", "api", **old)
        self.e.team(self.anna, "announce", "Authorization is ready", **old)
        self.e.team(self.sanya, "join", "hackathon", "--as", "frontend")
        self.e.team(self.sanya, "sync")
        text = ctx_text(self.e.hook(self.sanya, "session-start", {}))
        self.assertNotIn("Available from the team (announced):", text)

    def test_announce_shown_in_status(self):
        self.join_all()
        self.e.team(self.anna, "announce", "Authorization is ready: POST /auth/login")
        self.e.team(self.sanya, "sync")
        out = self.e.team(self.sanya, "status").stdout
        self.assertIn("ready: Authorization is ready: POST /auth/login", out)

    def test_fresh_items_count_as_recent(self):
        # an age of exactly 0 s must not be treated as "unknown/ancient"
        t = {"TEAM_SYNC_NOW": "2030-01-01T00:00:00Z"}
        self.e.team(self.anna, "join", "hackathon", "--as", "api", **t)
        self.e.team(self.anna, "change", "getUser() returns null", "--breaking", **t)
        self.e.team(self.anna, "update", "--status", "done", "--handoff", "auth refactor merged", **t)
        self.e.hook(self.anna, "session-end", {}, **t)
        self.e.team(self.sanya, "join", "hackathon", "--as", "frontend", **t)
        text = ctx_text(self.e.hook(self.sanya, "session-start", {}, **t))
        self.assertIn("Breaking changes in the last 24h", text)
        self.assertIn("Handoffs from agents no longer active:", text)
        self.assertIn("auth refactor merged", text)


if __name__ == "__main__":
    unittest.main()
