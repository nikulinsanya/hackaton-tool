"""
Session-simulation end-to-end tests for team-sync.

Unlike test_team.py (which calls `team.py hook <event>` directly), these tests
drive every hook through hooks.json exactly as Claude Code would: they load the
plugin's hook wiring, match PreToolUse/PostToolUse entries by tool name regex,
and run the resulting shell command with the environment Claude Code sets
(CLAUDE_PLUGIN_ROOT, CLAUDE_PROJECT_DIR, the event payload on stdin).

Run with:

    python3 -m unittest discover -s tests -v
    python3 -m unittest tests.test_sessions.SessionTests.test_full_lifecycle_one_agent
"""
import json
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # so `python3 -m unittest tests.test_sessions...` finds test_team too
from test_team import Env, git, ctx_text  # noqa: E402

PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, "..", "plugins", "team-sync"))
TEAM = os.path.join(PLUGIN_ROOT, "scripts", "team.py")
HOOKS_JSON = os.path.join(PLUGIN_ROOT, "hooks", "hooks.json")


def _q(s):
    """Shell-quote a CLI argument the way a human/agent typing the team CLI would:
    bare if safe, double-quoted otherwise (matches the pre-bash auto-approve tests
    in test_team.py, which accept double-quoted arguments)."""
    s = str(s)
    if re.match(r"^[A-Za-z0-9_\-./:]+$", s):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ------------------------------------------------------------------ simulator


class HookRunner:
    """Loads hooks.json and runs hook commands the way Claude Code does."""

    def __init__(self):
        with open(HOOKS_JSON, "r", encoding="utf-8") as f:
            self.spec = json.load(f)

    def _entries(self, event):
        return self.spec.get("hooks", {}).get(event, [])

    def _matcher_hits(self, matcher, tool_name):
        if not matcher or matcher == "*":
            return True
        if tool_name is None:
            return False
        return re.fullmatch(matcher, tool_name) is not None

    def run(self, event, payload, cwd, env, tool_name=None):
        """Runs every hooks.json entry for `event` whose matcher fits `tool_name`.
        Returns the list of parsed outputs (None for empty stdout)."""
        outs = []
        for entry in self._entries(event):
            matcher = entry.get("matcher")
            if "matcher" in entry and not self._matcher_hits(matcher, tool_name):
                continue
            for h in entry.get("hooks", []):
                outs.append(self._run_one(h, event, payload, cwd, env))
        return outs

    def _run_one(self, h, event, payload, cwd, env):
        e = dict(env)
        e["CLAUDE_PLUGIN_ROOT"] = PLUGIN_ROOT
        e["CLAUDE_PROJECT_DIR"] = cwd
        p = subprocess.run(
            ["sh", "-c", h["command"]], cwd=cwd, env=e,
            input=json.dumps(payload), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=h.get("timeout", 30),
        )
        assert p.returncode == 0, "{} ({}) exited {}: {}\n{}".format(
            event, h["command"], p.returncode, p.stderr, p.stdout)
        out = None
        if p.stdout.strip():
            out = json.loads(p.stdout)  # must be valid JSON
            self._check_contract(event, out)
        return out

    def _check_contract(self, event, out):
        if event == "Stop":
            # Stop uses the top-level Claude Code Stop-hook format, not hookSpecificOutput.
            self_keys = set(out.keys())
            assert self_keys == {"decision", "reason"}, out
            assert out.get("decision") == "block", out
            reason = out.get("reason")
            assert isinstance(reason, str) and reason, out
            return
        hso = out.get("hookSpecificOutput", {})
        assert hso.get("hookEventName") == event, (event, hso)
        ctx = hso.get("additionalContext")
        if ctx is not None:
            assert isinstance(ctx, str)
        if event == "PreToolUse":
            pd = hso.get("permissionDecision")
            if pd is not None:
                assert pd in ("allow", "deny", "ask"), pd
                reason = hso.get("permissionDecisionReason")
                assert isinstance(reason, str) and reason, reason
        if event == "SessionEnd":
            assert ctx is None


class Result:
    """Small result bag returned by every Session action."""

    def __init__(self, raw, decision=None, reason=None, context=None, post=None,
                 post_context=None, stopped=False, returncode=None, stdout=None, stderr=None):
        self.raw = raw
        self.decision = decision
        self.reason = reason
        self.context = context if context is not None else ctx_text(raw)
        self.post = post
        self.post_context = post_context if post_context is not None else ctx_text(post)
        self.stopped = stopped
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Session:
    """One simulated Claude Code session in one working directory (worktree or
    plain checkout), driven entirely through hooks.json."""

    def __init__(self, env_obj, cwd, session_id, runner=None, **team_env):
        self.e = env_obj
        self.cwd = cwd
        self.session_id = session_id
        self.runner = runner or HookRunner()
        self.team_env = team_env
        self.transcript_path = os.path.join(
            self.e.tmp, "transcripts", session_id + ".jsonl")

    def _env(self):
        return self.e.env(**self.team_env)

    def _payload(self, event_name, **extra):
        p = {
            "session_id": self.session_id,
            "transcript_path": self.transcript_path,
            "cwd": self.cwd,
            "hook_event_name": event_name,
        }
        p.update(extra)
        return p

    def _first(self, outs):
        return outs[0] if outs else None

    def start(self, source="startup"):
        payload = self._payload("SessionStart", source=source)
        out = self._first(self.runner.run("SessionStart", payload, self.cwd, self._env()))
        return Result(out)

    def prompt(self, text):
        payload = self._payload("UserPromptSubmit", prompt=text)
        out = self._first(self.runner.run("UserPromptSubmit", payload, self.cwd, self._env()))
        return Result(out)

    def _pre_tool(self, tool_name, tool_input):
        payload = self._payload("PreToolUse", tool_name=tool_name, tool_input=tool_input)
        return self._first(self.runner.run(
            "PreToolUse", payload, self.cwd, self._env(), tool_name=tool_name))

    def _post_tool(self, tool_name, tool_input, tool_response):
        payload = self._payload("PostToolUse", tool_name=tool_name, tool_input=tool_input,
                                tool_response=tool_response)
        return self._first(self.runner.run(
            "PostToolUse", payload, self.cwd, self._env(), tool_name=tool_name))

    def edit(self, rel_path, tool="Edit", approve=True, content=None):
        abs_path = os.path.join(self.cwd, rel_path)
        if tool == "Write":
            tool_input = {"file_path": abs_path, "content": content or "// written\n"}
        else:
            tool_input = {"file_path": abs_path, "old_string": "", "new_string":
                          content or "// edited by {}\n".format(self.session_id)}
        pre = self._pre_tool(tool, tool_input)
        hso = (pre or {}).get("hookSpecificOutput") or {}
        decision = hso.get("permissionDecision")
        reason = hso.get("permissionDecisionReason")
        pre_ctx = hso.get("additionalContext")
        if decision == "deny" or (decision == "ask" and not approve):
            return Result(pre, decision=decision, reason=reason, context=pre_ctx, stopped=True)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        if tool == "Write":
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(tool_input["content"])
        else:
            with open(abs_path, "a", encoding="utf-8") as f:
                f.write(tool_input["new_string"])
        tool_response = {"filePath": abs_path, "success": True}
        post = self._post_tool(tool, tool_input, tool_response)
        post_ctx = ctx_text(post)
        return Result(pre, decision=decision, reason=reason, context=pre_ctx,
                      post=post, post_context=post_ctx)

    def skill(self, skill_name):
        return self._pre_tool("Skill", {"skill": skill_name})

    def bash(self, cmd, approve=True):
        tool_input = {"command": cmd}
        pre = self._pre_tool("Bash", tool_input)
        hso = (pre or {}).get("hookSpecificOutput") or {}
        decision = hso.get("permissionDecision")
        reason = hso.get("permissionDecisionReason")
        pre_ctx = hso.get("additionalContext")
        if decision == "deny" or (decision == "ask" and not approve):
            return Result(pre, decision=decision, reason=reason, context=pre_ctx, stopped=True)
        p = subprocess.run(["sh", "-c", cmd], cwd=self.cwd, env=self._env(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        tool_response = {"stdout": p.stdout, "stderr": p.stderr, "returncode": p.returncode}
        post = self._post_tool("Bash", tool_input, tool_response)
        return Result(pre, decision=decision, reason=reason, context=pre_ctx, post=post,
                      post_context=ctx_text(post), returncode=p.returncode,
                      stdout=p.stdout, stderr=p.stderr)

    def team(self, *args, **kwargs):
        """Runs a team CLI call the way an agent reporting status would: as a Bash
        tool call, so it goes through the pre-bash auto-approve path."""
        cmd = 'python3 "{}" {}'.format(os.path.abspath(TEAM), " ".join(_q(a) for a in args))
        return self.bash(cmd, **kwargs)

    def stop(self, stop_hook_active=False):
        payload = self._payload("Stop", stop_hook_active=stop_hook_active)
        out = self._first(self.runner.run("Stop", payload, self.cwd, self._env()))
        reason = (out or {}).get("reason", "")
        return Result(out, decision=(out or {}).get("decision"), reason=reason, context=reason)

    def end(self, reason="prompt_input_exit"):
        payload = self._payload("SessionEnd", reason=reason)
        out = self._first(self.runner.run("SessionEnd", payload, self.cwd, self._env()))
        return Result(out)


def _show_json(repo_path, ref, path):
    p = subprocess.run(["git", "show", "{}:{}".format(ref, path)], cwd=repo_path,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except ValueError:
        return None


def branch_doc(repo_path, room, agent_id, ref="team-sync"):
    return _show_json(repo_path, ref, "rooms/{}/agents/{}.json".format(room, agent_id))


def room_doc(repo_path, room, ref="team-sync"):
    return _show_json(repo_path, ref, "rooms/{}/room.json".format(room))


def set_cfg(path, **cfg):
    with open(os.path.join(path, ".team-sync.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f)


FAST_CFG = {"fetch_interval_sec": 0, "digest_interval_sec": 0}


# ----------------------------------------------------------------------- tests


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.runner = HookRunner()
        self.sanya = self.e.clone("sanya")
        self.sanya_tests = self.e.worktree(self.sanya, "sanya-tests-wt")
        self.anna = self.e.clone("anna")

    def tearDown(self):
        self.e.cleanup()

    def session(self, cwd, sid, **team_env):
        return Session(self.e, cwd, sid, runner=self.runner, **team_env)

    # 1. ---------------------------------------------------------- lifecycle

    def test_full_lifecycle_one_agent(self):
        set_cfg(self.sanya, **FAST_CFG)
        s = self.session(self.sanya, "sess-1", CLAUDE_TEAM_ROOM="hackathon",
                         CLAUDE_TEAM_AGENT="frontend")
        revs = []

        r = s.start()
        self.assertIn("You are agent", r.context)
        doc = branch_doc(self.e.origin, "hackathon", "sanya-frontend")
        self.assertIsNotNone(doc)
        self.assertEqual(doc["state"], "online")
        revs.append(doc["rev"])

        r = s.prompt("let's start")  # nothing changed since start: no digest

        r = s.team("update", "--status", "in_progress", "--working-on", "login form")
        self.assertEqual(r.decision, "allow")  # pre-bash auto-approved, no prompt
        self.assertEqual(r.returncode, 0)
        doc = branch_doc(self.e.origin, "hackathon", "sanya-frontend")
        self.assertEqual(doc["working_on"], "login form")
        self.assertEqual(doc["status"], "in_progress")
        revs.append(doc["rev"])

        r = s.edit("src/auth/login.ts")
        self.assertIsNone(r.decision)  # no overlap: nothing to approve
        s.team("sync")  # forced sync point
        doc_local = branch_doc(self.sanya, "hackathon", "sanya-frontend")
        self.assertTrue(any(t["path"] == "src/auth/login.ts" and t["source"] == "auto"
                           for t in doc_local["touching"]))
        doc = branch_doc(self.e.origin, "hackathon", "sanya-frontend")
        self.assertTrue(any(t["path"] == "src/auth/login.ts" for t in doc["touching"]))
        revs.append(doc["rev"])

        s.end()
        doc = branch_doc(self.e.origin, "hackathon", "sanya-frontend")
        self.assertEqual(doc["state"], "offline")
        revs.append(doc["rev"])

        self.assertEqual(revs, sorted(set(revs)))
        self.assertEqual(len(revs), len(set(revs)))
        for a, b in zip(revs, revs[1:]):
            self.assertLess(a, b)

    # 2. -------------------------------------------------------- demo script

    def test_demo_script_breaking_change_and_ask(self):
        for cwd in (self.sanya, self.sanya_tests, self.anna):
            set_cfg(cwd, overlap_action="ask", **FAST_CFG)
        api = self.session(self.anna, "api-sess", CLAUDE_TEAM_ROOM="hackathon",
                           CLAUDE_TEAM_AGENT="api")
        frontend = self.session(self.sanya, "fe-sess", CLAUDE_TEAM_ROOM="hackathon",
                                CLAUDE_TEAM_AGENT="frontend")
        tests_agent = self.session(self.sanya_tests, "tests-sess", CLAUDE_TEAM_ROOM="hackathon",
                                   CLAUDE_TEAM_AGENT="tests")

        api.start()
        frontend.start()
        tests_agent.start()

        api.team("claim", "src/auth/")
        api.team("change", "getUser() returns null instead of throwing", "--breaking")

        d = frontend.prompt("what's new?")
        self.assertIn("BREAKING", d.context)
        self.assertIn("getUser()", d.context)

        r = frontend.edit("src/auth/login.ts", approve=True)
        self.assertEqual(r.decision, "ask")
        self.assertFalse(r.stopped)
        doc_local = branch_doc(self.sanya, "hackathon", "sanya-frontend")
        self.assertTrue(any(t["path"] == "src/auth/login.ts" for t in doc_local["touching"]))

        frontend.team("ask", "anna-api", "can you share a fixture for getUser?")

        d = api.prompt("anything for me?")
        self.assertIn("ASK for you from sanya-frontend", d.context)

        api.team("reply", "sanya-frontend#a1", "fixture added in test/fixtures", "--done")

        d = frontend.prompt("checking replies")
        self.assertIn("anna-api replied to your ask", d.context)

        # final state on origin
        fe_doc = branch_doc(self.e.origin, "hackathon", "sanya-frontend")
        api_doc = branch_doc(self.e.origin, "hackathon", "anna-api")
        self.assertTrue(any(a["to"] == "anna-api" for a in fe_doc["asks"]))
        self.assertTrue(any(r["ask"] == "sanya-frontend#a1" and r["done"] for r in api_doc["replies"]))

        # tests agent (same machine as frontend) sees it without the network
        out = self.e.team(self.sanya_tests, "status", "--json", CLAUDE_TEAM_REMOTE="none",
                         CLAUDE_TEAM_ROOM="hackathon", CLAUDE_TEAM_AGENT="tests").stdout
        docs = {dd["id"]: dd for dd in json.loads(out)}
        self.assertIn("anna-api", docs)  # synced to origin earlier, visible via local branch too
        self.assertEqual(docs["sanya-frontend"]["status"], fe_doc["status"])

    # 3. --------------------------------------------------------- mid digest

    def test_mid_turn_digest(self):
        # v0.3: PostToolUse never injects a digest; updates arrive on the next
        # prompt (or, for breaking changes, at end of turn via the Stop hook).
        set_cfg(self.sanya, **FAST_CFG)
        set_cfg(self.anna, **FAST_CFG)
        frontend = self.session(self.sanya, "fe-long", CLAUDE_TEAM_ROOM="hackathon",
                                CLAUDE_TEAM_AGENT="frontend")
        self.e.team(self.anna, "join", "hackathon", "--as", "api")
        frontend.start()
        frontend.prompt("start")

        r1 = frontend.edit("src/api/routes.ts")
        self.assertIn("--breaking", r1.post_context)  # first edit of the turn: announce reminder

        self.e.team(self.anna, "change", "renamed getUser to fetchUser")
        self.e.team(self.anna, "sync")

        r2 = frontend.edit("src/auth/session.ts")
        self.assertEqual(r2.post_context, "")  # not the first edit of the turn: nothing, no digest either

        r3 = frontend.prompt("what changed?")
        self.assertIn("renamed getUser to fetchUser", r3.context)

        r4 = frontend.edit("README.md")
        self.assertIn("--breaking", r4.post_context)  # first edit of the *new* turn: reminder again
        r5 = frontend.prompt("again?")
        self.assertEqual(r5.context, "")  # nothing new since r3

    # 4. ---------------------------------------------- resume/compact/clear

    def test_resume_compact_clear_sessions(self):
        set_cfg(self.sanya, **FAST_CFG)
        set_cfg(self.anna, **FAST_CFG)
        self.e.team(self.anna, "join", "hackathon", "--as", "api")
        s1 = self.session(self.sanya, "sid-1", CLAUDE_TEAM_ROOM="hackathon",
                          CLAUDE_TEAM_AGENT="frontend")
        s1.start(source="startup")

        self.e.team(self.anna, "update", "--status", "in_progress", "--working-on", "POST /session")
        self.e.team(self.anna, "sync")

        r = s1.start(source="resume")  # re-briefs with current room state
        self.assertIn("POST /session", r.context)

        self.e.team(self.anna, "update", "--working-on", "POST /session v2")
        self.e.team(self.anna, "sync")
        r = s1.start(source="compact")
        self.assertIn("POST /session v2", r.context)

        # a new session id after /clear gets a fresh briefing, not a digest
        s2 = self.session(self.sanya, "sid-2", CLAUDE_TEAM_ROOM="hackathon",
                          CLAUDE_TEAM_AGENT="frontend")
        r = s2.start(source="clear")
        self.assertIn("You are agent", r.context)

        # session-local "seen" state doesn't leak between session ids: a change
        # that happened before s2 ever looked is not re-announced to s1 either,
        # but a *new* change after both have started is seen independently by each
        self.e.team(self.anna, "update", "--working-on", "POST /session v3")
        self.e.team(self.anna, "sync")
        d1 = s1.prompt("x")
        d2 = s2.prompt("x")
        self.assertIn("POST /session v3", d1.context)
        self.assertIn("POST /session v3", d2.context)
        # second look: nothing new for either
        self.assertEqual(ctx_text(s1.prompt("y").raw), "")
        self.assertEqual(ctx_text(s2.prompt("y").raw), "")

    # 5. --------------------------------------------------------- join mid

    def test_join_mid_session(self):
        s = self.session(self.sanya, "mid-sess")  # no identity at all
        r = s.start()
        self.assertEqual(r.context, "")
        self.assertIsNone(r.raw)
        r = s.prompt("hi")
        self.assertIsNone(r.raw)
        r = s.edit("README.md")
        self.assertIsNone(r.decision)
        self.assertEqual(r.post_context, "")
        self.assertEqual(git(self.sanya, "branch", "--list", "team-sync"), "")

        r = s.team("join", "hackathon", "--as", "x")
        self.assertEqual(r.returncode, 0)

        r = s.prompt("now what")
        self.assertIn("You are agent", r.context)

    def test_first_prompt_without_session_start_publishes_agent(self):
        # a plugin enabled through project settings loads after the session started,
        # so SessionStart never fires: the first prompt must brief AND publish the agent
        s = self.session(self.sanya, "late-plugin", CLAUDE_TEAM_ROOM="hackathon",
                         CLAUDE_TEAM_AGENT="frontend")
        r = s.prompt("hi")
        self.assertIn("You are agent", r.context)
        doc = branch_doc(self.e.origin, "hackathon", "sanya-frontend")
        self.assertIsNotNone(doc)
        self.assertEqual(doc["state"], "online")
        log = git(self.sanya, "log", "--format=%s", "team-sync")
        self.assertIn("sanya-frontend: session start", log)
        # the second prompt is an ordinary digest, not a second briefing
        r = s.prompt("again")
        self.assertNotIn("You are agent", r.context)

    # 6. -------------------------------------------------------- worktree layout

    def test_claude_code_worktree_layout(self):
        set_cfg(self.sanya, **FAST_CFG)
        wt_path = os.path.join(self.sanya, ".claude", "worktrees", "frontend")
        os.makedirs(os.path.dirname(wt_path), exist_ok=True)
        git(self.sanya, "worktree", "add", "-q", "-b", "frontend", wt_path)
        set_cfg(wt_path, **FAST_CFG)

        wt_session = self.session(wt_path, "wt-sess", CLAUDE_TEAM_ROOM="hackathon",
                                  CLAUDE_TEAM_REMOTE="none")
        r = wt_session.start()
        self.assertIn('You are agent "sanya-frontend"', r.context)
        wt_session.team("update", "--status", "in_progress", "--working-on", "nav bar")

        main_session = self.session(self.sanya, "main-sess", CLAUDE_TEAM_ROOM="hackathon",
                                    CLAUDE_TEAM_AGENT="backend", CLAUDE_TEAM_REMOTE="none")
        r = main_session.start()
        self.assertIn("sanya-frontend", r.context)
        self.assertIn("nav bar", r.context)

    # 7. ----------------------------------------------------- presence

    def test_presence_across_machines(self):
        for cwd in (self.sanya, self.sanya_tests, self.anna):
            set_cfg(cwd, **FAST_CFG)
        frontend = self.session(self.sanya, "fe", CLAUDE_TEAM_ROOM="hackathon",
                                CLAUDE_TEAM_AGENT="frontend")
        tests_agent = self.session(self.sanya_tests, "te", CLAUDE_TEAM_ROOM="hackathon",
                                   CLAUDE_TEAM_AGENT="tests")
        api = self.session(self.anna, "ap", CLAUDE_TEAM_ROOM="hackathon", CLAUDE_TEAM_AGENT="api")

        frontend.start()
        tests_agent.start()
        api.start()
        frontend.team("update", "--status", "in_progress", "--working-on", "login form")
        tests_agent.end()  # offline
        api.team("update", "--status", "in_progress", "--working-on", "POST /session")

        for cwd in (self.sanya, self.sanya_tests, self.anna):
            out = self.e.team(cwd, "status", "--json", "--all").stdout
            docs = {dd["id"]: dd for dd in json.loads(out)}
            for aid in ("sanya-frontend", "sanya-tests", "anna-api"):
                origin_doc = branch_doc(self.e.origin, "hackathon", aid)
                self.assertEqual(docs[aid]["status"], origin_doc["status"], (cwd, aid))
                self.assertEqual(docs[aid]["state"], origin_doc["state"], (cwd, aid))
            self.assertEqual(docs["sanya-tests"]["_presence"], "offline")
            self.assertEqual(docs["sanya-frontend"]["_presence"], "online")

        # stale: frontend stays "online" in its doc but no heartbeat for a long time
        future = self.e.team(self.sanya, "status", "--json", TEAM_SYNC_NOW="2099-01-01T00:00:00Z",
                            CLAUDE_TEAM_ROOM="hackathon", CLAUDE_TEAM_AGENT="frontend").stdout
        docs = {dd["id"]: dd for dd in json.loads(future)}
        self.assertEqual(docs["sanya-frontend"]["_presence"], "stale")

    # 8. ------------------------------------------------------------ handoff

    def test_handoff_surfacing(self):
        set_cfg(self.sanya, **FAST_CFG)
        set_cfg(self.anna, **FAST_CFG)
        a = self.session(self.anna, "a-sess", CLAUDE_TEAM_ROOM="hackathon", CLAUDE_TEAM_AGENT="api")
        a.start()
        a.team("update", "--status", "done", "--handoff", "fixture lives in test/fixtures/user.json")
        a.end()

        b = self.session(self.sanya, "b-sess", CLAUDE_TEAM_ROOM="hackathon", CLAUDE_TEAM_AGENT="frontend")
        r = b.start()
        # v0.2 fix: a handoff from an agent that then went offline used to never
        # reach teammates (the briefing only listed *online* agents, and the
        # handoff is written right before the agent goes offline). The briefing
        # now has a dedicated section for handoffs from inactive agents.
        self.assertIn("Handoffs from agents no longer active:", r.context)
        self.assertIn("fixture lives in test/fixtures/user.json", r.context)
        self.assertIn("anna-api", r.context)
        # it is also visible through `team status` (format_agent lists every agent,
        # online or not, and shows handoff for status == done)
        out = self.e.team(self.sanya, "status", CLAUDE_TEAM_ROOM="hackathon",
                         CLAUDE_TEAM_AGENT="frontend").stdout
        self.assertIn("fixture lives in test/fixtures/user.json", out)

        # a *new* handoff from another room agent surfaces once in the digest
        c = self.session(self.anna, "c-sess", CLAUDE_TEAM_ROOM="hackathon", CLAUDE_TEAM_AGENT="db")
        c.start()
        c.team("update", "--status", "done", "--handoff", "schema migration is in db/migrate.sql")
        d = b.prompt("anything new?")
        self.assertIn("HANDOFF from anna-db: schema migration is in db/migrate.sql", d.context)
        d2 = b.prompt("again?")
        self.assertNotIn("HANDOFF", d2.context)

    # 9. --------------------------------------------------------- room config

    def test_room_config_briefing_and_digest(self):
        set_cfg(self.sanya, **FAST_CFG)
        set_cfg(self.anna, **FAST_CFG)
        api = self.session(self.anna, "api-sess", CLAUDE_TEAM_ROOM="hackathon",
                           CLAUDE_TEAM_AGENT="api")
        api.start()
        api.team("room", "create", "hackathon", "--title", "Hack Day",
                 "--description", "Build stuff", "--goal", "Ship it")
        api.team("room", "set", "track", "dashboard")
        api.team("room", "rule", "add", "Use zod for validation")
        api.team("room", "set", "policy.overlap_action", "warn")

        frontend = self.session(self.sanya, "fe-sess", CLAUDE_TEAM_ROOM="hackathon",
                                CLAUDE_TEAM_AGENT="frontend")
        r = frontend.start()
        self.assertIn("Room goal: Ship it", r.context)
        self.assertIn("Room: Hack Day", r.context)
        self.assertIn("About: Build stuff", r.context)
        self.assertIn("Room rules (follow these):", r.context)
        self.assertIn("Use zod for validation", r.context)
        self.assertIn("Room info:", r.context)
        self.assertIn("track: dashboard", r.context)
        self.assertIn("Room policy: overlap_action=warn (set by anna-api)", r.context)

        # a config change by another agent shows once in the next prompt digest...
        api.team("room", "set", "title", "Hack Day 2")
        d = frontend.prompt("what's new?")
        self.assertIn("ROOM: title = Hack Day 2 (by anna-api)", d.context)
        d2 = frontend.prompt("again?")
        self.assertEqual(d2.context, "")

        # config changes also never arrive mid-turn (v0.3): only on the next prompt
        api.team("room", "set", "title", "Hack Day 3")
        r1 = frontend.edit("src/api/routes.ts")
        self.assertNotIn("Hack Day 3", r1.post_context)  # no config digest mid-turn
        d3 = frontend.prompt("config change?")
        self.assertIn("ROOM: title = Hack Day 3 (by anna-api)", d3.context)

    def test_room_pre_bash_asks_for_policy_and_from(self):
        set_cfg(self.sanya, **FAST_CFG)
        s = self.session(self.sanya, "pb-sess", CLAUDE_TEAM_ROOM="hackathon",
                         CLAUDE_TEAM_AGENT="frontend")
        s.start()
        self.assertEqual(s.team("room", "set", "policy.overlap_action", "warn",
                                approve=False).decision, "ask")
        self.assertEqual(s.team("room", "unset", "policy.overlap_action",
                                approve=False).decision, "ask")
        missing = os.path.join(self.e.tmp, "missing.json")
        self.assertEqual(s.team("room", "create", "x", "--from", missing, approve=False).decision,
                         "ask")
        no_policy = os.path.join(self.e.tmp, "tmpl.json")
        with open(no_policy, "w") as f:
            json.dump({"title": "T"}, f)
        self.assertEqual(s.team("room", "create", "x", "--from", no_policy).decision, "allow")
        with_policy = os.path.join(self.e.tmp, "tmpl2.json")
        with open(with_policy, "w") as f:
            json.dump({"title": "T", "policy": {"overlap_action": "warn"}}, f)
        self.assertEqual(s.team("room", "create", "x", "--from", with_policy,
                                approve=False).decision, "ask")
        self.assertEqual(s.team("room", "set", "title", "X").decision, "allow")
        self.assertEqual(s.team("room", "rule", "add", "a rule").decision, "allow")

    # 10a. --------------------------------------------------------- stop hook

    def test_stop_hook_incoming_breaking_change(self):
        set_cfg(self.sanya, **FAST_CFG)
        set_cfg(self.anna, **FAST_CFG)
        frontend = self.session(self.sanya, "fe-stop", CLAUDE_TEAM_ROOM="hackathon",
                                CLAUDE_TEAM_AGENT="frontend")
        api = self.session(self.anna, "api-stop", CLAUDE_TEAM_ROOM="hackathon",
                           CLAUDE_TEAM_AGENT="api")
        frontend.start()
        api.start()

        frontend.prompt("start task")
        r = frontend.edit("src/auth/login.ts")
        self.assertIn("--breaking", r.post_context)  # first-edit announce reminder, no digest

        api.team("change", "getUser() returns null", "--breaking")

        r1 = frontend.stop()
        self.assertEqual(r1.decision, "block")
        self.assertIn("anna-api", r1.reason)
        self.assertIn("impact check", r1.reason)

        # loop-safe: Claude Code re-invokes Stop with stop_hook_active=True
        r2 = frontend.stop(stop_hook_active=True)
        self.assertIsNone(r2.raw)

        # the breaking change is not repeated, but other items posted meanwhile are
        api.team("learn", "staging needs VPN")
        d = frontend.prompt("next")
        self.assertNotIn("getUser", d.context)
        self.assertIn("staging needs VPN", d.context)

    def test_resume_does_not_repeat_reported_breaking_change(self):
        set_cfg(self.sanya, **FAST_CFG)
        set_cfg(self.anna, **FAST_CFG)
        frontend = self.session(self.sanya, "fe-resume", CLAUDE_TEAM_ROOM="hackathon",
                                CLAUDE_TEAM_AGENT="frontend")
        api = self.session(self.anna, "api-resume", CLAUDE_TEAM_ROOM="hackathon",
                           CLAUDE_TEAM_AGENT="api")
        frontend.start()
        api.start()
        api.team("change", "getUser() returns null", "--breaking")
        d = frontend.prompt("next")  # reported here, with the warn instruction
        self.assertIn("getUser() returns null", d.context)
        self.assertIn("impact check", d.context)
        # resume and auto-compact keep the session id: listed, but not to be repeated
        for source in ("resume", "compact"):
            b = frontend.start(source=source).context
            self.assertIn("already reported to your human in this session", b)
            self.assertIn("getUser() returns null", b)
            self.assertNotIn("Breaking changes in the last 24h", b)
        # a new session (e.g. after /clear) has not told its human yet: warn again
        fresh = self.session(self.sanya, "fe-new-session", CLAUDE_TEAM_ROOM="hackathon",
                             CLAUDE_TEAM_AGENT="frontend")
        b = fresh.start().context
        self.assertIn("Breaking changes in the last 24h", b)
        self.assertIn("impact check", b)

    def test_stop_hook_new_agent_still_announced_as_joined(self):
        # a teammate whose very first visible action is a breaking change, surfaced at
        # Stop: the next digest must still say they joined, without repeating the change
        set_cfg(self.sanya, **FAST_CFG)
        set_cfg(self.anna, **FAST_CFG)
        frontend = self.session(self.sanya, "fe-new", CLAUDE_TEAM_ROOM="hackathon",
                                CLAUDE_TEAM_AGENT="frontend")
        frontend.start()
        frontend.prompt("start task")
        api = self.session(self.anna, "api-new", CLAUDE_TEAM_ROOM="hackathon",
                           CLAUDE_TEAM_AGENT="api")
        api.start()
        api.team("change", "getUser() returns null", "--breaking")
        r = frontend.stop()
        self.assertIn("getUser() returns null", r.reason)
        d = frontend.prompt("next")
        self.assertIn("anna-api joined", d.context)
        self.assertNotIn("getUser", d.context)

    def test_post_tool_first_edit_breaking_reminder(self):
        """The announce-breaking reminder lives on the first edit of a turn
        (PostToolUse), not at Stop: it must never duplicate or replace the agent's
        real answer."""
        set_cfg(self.sanya, **FAST_CFG)

        s1 = self.session(self.sanya, "own-edit-1", CLAUDE_TEAM_ROOM="hackathon",
                          CLAUDE_TEAM_AGENT="frontend1")
        s1.start()
        s1.prompt("task")
        r1 = s1.edit("src/auth/login.ts")
        self.assertIn("Before your final answer", r1.post_context)
        self.assertIn("--breaking", r1.post_context)

        # a later edit in the same turn: no repeat
        r1b = s1.edit("src/auth/session.ts")
        self.assertEqual(r1b.post_context, "")

        # a new turn: the reminder fires again at its first edit
        s1.prompt("task 2")
        r1c = s1.edit("README.md")
        self.assertIn("--breaking", r1c.post_context)

        # Stop never blocks for this (moved to PostToolUse): no duplicate/second answer
        r1d = s1.stop()
        self.assertIsNone(r1d.raw)

    def test_stop_hook_breaking_check_disabled(self):
        set_cfg(self.sanya, **FAST_CFG)
        set_cfg(self.anna, **FAST_CFG)
        s = self.session(self.sanya, "bc-off", CLAUDE_TEAM_ROOM="hackathon",
                         CLAUDE_TEAM_AGENT="frontend")
        s.start()
        s.team("room", "create", "hackathon", "--title", "T")
        s.team("room", "set", "policy.breaking_check", "false")

        s.prompt("task")
        r1 = s.edit("src/auth/login.ts")
        self.assertEqual(r1.post_context, "")  # first-edit reminder suppressed
        r1s = s.stop()
        self.assertIsNone(r1s.raw)

        # incoming breaking changes still work: announced *after* the next prompt's
        # digest, so Stop (not the ordinary digest) is what surfaces it
        api = self.session(self.anna, "api-bc", CLAUDE_TEAM_ROOM="hackathon",
                           CLAUDE_TEAM_AGENT="api")
        api.start()

        s.prompt("next")
        api.team("change", "x breaks", "--breaking")
        s.edit("src/auth/session.ts")
        r2 = s.stop()
        self.assertEqual(r2.decision, "block")
        self.assertIn("anna-api", r2.reason)
        self.assertNotIn("--breaking", r2.reason)  # no own-edit reminder text in Stop

    def test_stop_hook_observer_incoming_only(self):
        set_cfg(self.sanya, **FAST_CFG)
        set_cfg(self.anna, **FAST_CFG)
        obs = self.session(self.sanya, "obs-sess", CLAUDE_TEAM_ROOM="hackathon",
                           CLAUDE_TEAM_AGENT="watcher", CLAUDE_TEAM_ROLE="observer")
        obs.start()
        obs.prompt("task")
        r0 = obs.edit("src/auth/login.ts")
        self.assertEqual(r0.post_context, "")  # observers are never reminded to announce changes
        r1 = obs.stop()
        self.assertIsNone(r1.raw)

        api = self.session(self.anna, "api-obs", CLAUDE_TEAM_ROOM="hackathon",
                           CLAUDE_TEAM_AGENT="api")
        api.start()

        obs.prompt("next")
        api.team("change", "breaks something", "--breaking")
        obs.edit("README.md")
        r2 = obs.stop()
        self.assertEqual(r2.decision, "block")
        self.assertIn("anna-api", r2.reason)

    def test_stop_hook_not_joined(self):
        s = self.session(self.sanya, "nj-sess")
        s.prompt("hi")
        r = s.stop()
        self.assertIsNone(r.raw)

    def test_pre_skill_hook_wiring(self):
        set_cfg(self.sanya, **FAST_CFG)
        s = self.session(self.sanya, "skill-sess", CLAUDE_TEAM_ROOM="hackathon",
                         CLAUDE_TEAM_AGENT="frontend")
        s.start()
        out = s.skill("team-sync:team-protocol")
        hso = (out or {}).get("hookSpecificOutput") or {}
        self.assertEqual(hso.get("permissionDecision"), "allow")

        out2 = s.skill("some-other-plugin:some-skill")
        self.assertIsNone(out2)

        not_joined = self.session(self.anna, "not-joined-skill")
        out3 = not_joined.skill("team-sync:team-protocol")
        self.assertIsNone(out3)

    # 10. --------------------------------------------------------- wiring

    def test_hooks_json_wiring(self):
        with open(HOOKS_JSON, "r", encoding="utf-8") as f:
            spec = json.load(f)
        with open(TEAM, "r", encoding="utf-8") as f:
            source = f.read()
        hook_names = set(re.findall(r'"([a-z-]+)":\s*h_', source))
        self.assertTrue(hook_names)
        seen_events = set()
        for event, entries in spec["hooks"].items():
            for entry in entries:
                for h in entry["hooks"]:
                    m = re.search(r"hook (\S+)", h["command"])
                    self.assertIsNotNone(m, h["command"])
                    self.assertIn(m.group(1), hook_names, "{} not a known hook handler".format(m.group(1)))
                seen_events.add(event)
        self.assertEqual(seen_events, {"SessionStart", "UserPromptSubmit", "PreToolUse",
                                       "PostToolUse", "Stop", "SessionEnd"})

        matchers = {entry.get("matcher") for entry in spec["hooks"]["PreToolUse"]}
        self.assertIn("Edit|Write|MultiEdit|NotebookEdit", matchers)
        self.assertIn("Bash", matchers)
        self.assertIn("Skill", matchers)
        for entry in spec["hooks"]["PreToolUse"]:
            if entry.get("matcher") == "Skill":
                self.assertTrue(
                    any("pre-skill" in h["command"] for h in entry["hooks"]))
        post_matchers = {entry.get("matcher") for entry in spec["hooks"]["PostToolUse"]}
        self.assertIn("Edit|Write|MultiEdit|NotebookEdit|Bash", post_matchers)

        # every hook exits 0 with empty output: outside any repo, and in a repo
        # where this agent never joined
        env = self.e.env()
        for event, entries in spec["hooks"].items():
            for entry in entries:
                for h in entry["hooks"]:
                    for cwd in (self.e.home, self.sanya):
                        e = dict(env)
                        e["CLAUDE_PLUGIN_ROOT"] = PLUGIN_ROOT
                        e["CLAUDE_PROJECT_DIR"] = cwd
                        p = subprocess.run(["sh", "-c", h["command"]], cwd=cwd, env=e,
                                          input=json.dumps({"cwd": cwd, "session_id": "wiring"}),
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                          timeout=h.get("timeout", 30))
                        self.assertEqual(p.returncode, 0, (cwd, h["command"], p.stderr))
                        self.assertEqual(p.stdout.strip(), "", (cwd, h["command"]))

    def test_announce_skill_wiring(self):
        skill_path = os.path.join(PLUGIN_ROOT, "skills", "announce", "SKILL.md")
        self.assertTrue(os.path.exists(skill_path))
        with open(skill_path, "r", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("disable-model-invocation: true", text)
        self.assertIn('Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" *)', text)
        # found in a real two-laptop test: announcing unpushed work leaves the other
        # agent with nothing to fetch, so both the /team-sync:announce skill and the
        # team-protocol reaction rules must say to check it's pushed first
        self.assertIn("pushed", text)
        self.assertIn("origin/<branch>..HEAD", text)
        with open(os.path.join(PLUGIN_ROOT, "skills", "team-protocol", "SKILL.md"),
                  encoding="utf-8") as f:
            protocol_text = f.read()
        self.assertIn("pushed", protocol_text)
        self.assertIn("needs pushing first", protocol_text)
        # the team pushes finished work straight to main; uncommitted work counts as unpushed,
        # and a receiving agent pulls main before using what was announced there
        self.assertIn("uncommitted", text)
        self.assertIn("Commit and push to main, then announce?", text)
        self.assertIn("straight to `main` by default", protocol_text)
        self.assertIn("git pull --rebase --autostash", protocol_text)

    def test_start_skill_defaults_to_current_room(self):
        # /team-sync:start without a room once configured "hackathon" while the agent was in
        # the room from the repo settings; the skill must resolve the room via whoami
        with open(os.path.join(PLUGIN_ROOT, "skills", "start", "SKILL.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("whoami", text)
        self.assertNotIn("default `hackathon`", text)
        out = self.e.team(self.sanya, "whoami", CLAUDE_TEAM_ROOM="hackathon-idea1").stdout
        self.assertEqual(json.loads(out)["room"], "hackathon-idea1")

    def test_start_and_join_skills_wire_name_the_room(self):
        # the lead names the room: /team-sync:start creates and joins it; /team-sync:join
        # without a name falls back to listing rooms with `team rooms`
        with open(os.path.join(PLUGIN_ROOT, "skills", "start", "SKILL.md"), encoding="utf-8") as f:
            start_text = f.read()
        self.assertIn("whoami", start_text)
        self.assertIn("join", start_text)
        with open(os.path.join(PLUGIN_ROOT, "skills", "join", "SKILL.md"), encoding="utf-8") as f:
            join_text = f.read()
        self.assertIn("rooms", join_text)

    # 11. -------------------------------------------------------- announce

    def test_announce_mid_turn_then_next_prompt(self):
        set_cfg(self.sanya, **FAST_CFG)
        set_cfg(self.anna, **FAST_CFG)
        frontend = self.session(self.sanya, "fe-announce", CLAUDE_TEAM_ROOM="hackathon",
                                CLAUDE_TEAM_AGENT="frontend")
        api = self.session(self.anna, "api-announce", CLAUDE_TEAM_ROOM="hackathon",
                           CLAUDE_TEAM_AGENT="api")
        frontend.start()
        api.start()

        frontend.prompt("start task")
        r = frontend.edit("src/auth/login.ts")
        self.assertIn("--breaking", r.post_context)  # first-edit announce reminder, no digest

        api.team("announce", "Authorization is ready: POST /auth/login")

        r1 = frontend.stop()
        # Stop never blocks for the own-edit reminder (moved to the first-edit
        # PostToolUse note) and never because of the READY announcement: that
        # waits for the next prompt.
        self.assertIsNone(r1.raw)

        d = frontend.prompt("anything new?")
        self.assertIn("* READY from anna-api", d.context)
        self.assertIn("Authorization is ready", d.context)


if __name__ == "__main__":
    unittest.main()
