# team-sync plan

## Where we are

**v0.1 is built, tested, and smoke-tested in real Claude Code (2.1.310, headless `-p`).**

- 17 end-to-end tests pass: two clones and an extra worktree, remote sync, divergent merges,
  parallel writers, overlap ask/deny, observer mode, digest of asks/replies/breaking changes,
  secret guard, CLI auto-approval.
- 9 session simulations (`tests/test_sessions.py`) drive the hooks through `hooks.json` like Claude Code
  does (startup → prompt → edit → bash → end, resume/compact/clear) and check the status files on the
  local branch and on origin: lifecycle, the demo script, mid-turn digest, join mid-session, the
  `claude -w` worktree layout, presence, handoff, hooks.json wiring. Green on stock macOS python 3.9.
- **v0.2.0 room configuration is done** (see below): 38 tests green on python 3.9 and 3.14.
- v0.4.2: fixes from a real headless Claude run (quote-aware CLI auto-approval, announce reminder
  at first edit instead of a second answer, briefing matches the protocol, team-sync skills load
  without a prompt).
- v0.4.3: a resumed or compacted session doesn't repeat breaking-change warnings it already gave;
  the announce/start skills run git and team commands one per call. Re-run of the real-Claude
  scenario after v0.4.2: all 10 steps pass, no permission prompts, one answer per turn.
- `claude plugin validate` passes for the marketplace and the plugin.
- Verified with real `claude -p --plugin-dir` sessions in the sandbox:
  - the SessionStart briefing reaches the agent (it quoted it and listed its open asks);
  - an edit inside another agent's claimed `src/auth/` was held by the hook, and Claude stopped,
    explained who was there and offered "edit anyway" or "ask them first";
  - the agent updated its status and answered an ask with `reply ... --done` via the CLI,
    without permission prompts (the `pre-bash` hook auto-approves plain CLI calls);
  - SessionEnd marked the agent offline.

## Milestone 1: finish verification (before the hackathon, ~1 h)

Only the interactive-UI parts remain unverified. Fix what breaks, add a test for each fix.

- [x] `claude plugin validate .` and `claude plugin validate plugins/team-sync` pass.
- [x] SessionStart briefing reaches the agent (headless).
- [x] Overlap edit is held, and the agent reacts per protocol (headless).
- [x] Agent publishes status and replies through the CLI without prompts (headless).
- [x] SessionEnd marks offline (headless).
- [-] Interactive sandbox checks (`tools/sandbox.sh`): **skipped** (Sanya, 2026-10-26). We verify in
      the real repo at kickoff instead.
- [-] Mid-turn digest: **dropped** in v0.3 (no updates in the middle of a task).
- [x] Push this repo to `git@gitlab.com:nikulinsanya/hackaton-tool.git`. The hackathon dogfoods *this* repo, so everyone runs
      `claude --plugin-dir <stable checkout>/plugins/team-sync` (point it at main, not the agent's own
      worktree, so an agent never runs its own half-edited `team.py`). No marketplace install needed.

## v0.2: room configuration (done)

A team lead (or any member) starts a hackathon by publishing the room's configuration; every
agent in the room gets it in its briefing and changes in its digest.

Decided (Sanya, 2026-10-25):
- **Live on the team branch**, optionally bootstrapped from a committed template file
  (`team room create <room> --from hackathon.json`, `team room export`).
- **Any member** may edit; observers may not. A human may also run it without joining.
- **v0.2 scope:** title, description, goal; room rules; room policy; dynamic custom fields that
  can be added/edited/removed later by command or by an agent's tool call. Schedule and tracks: later.
- **Stay on Python** (stdlib, 3.8+).

Design:
- One file per writer: `rooms/<room>/config/<writer-id>.json` (writer = agent id, or the owner when a
  human runs the CLI outside a room). Keeps the single-writer rule; v0.1 clients ignore the folder.
- Effective config = per key, the entry with the highest (Lamport `clock`, `at`, writer). A write sets
  `clock` = 1 + the highest clock seen for that key, so an edit made after seeing another always wins,
  regardless of clock skew. `null` value = unset (files are never deleted).
- Keys: `title`, `description`, `goal`; `rule.<id>`; `policy.<name>` (allowlist: overlap_action,
  overlap_granularity, stale_after_min, touch_ttl_min, digest_interval_sec, fetch_interval_sec;
  never `branch`/`remote`/`auto_observe`); anything else matching `[a-z][a-z0-9_-]{0,31}` is a custom field.
- Precedence: defaults < `.team-sync.json` < room policy < env.
- Policy changes are **not** auto-approved by the `pre-bash` hook: a human approves them. Everything
  else stays prompt-free for agents.
- Legacy `room.json` goal is read as the base layer; `team room --goal` keeps working.

- [x] CLI: `room [show]`, `room create <room> [--title --description --goal --from FILE]`,
      `room set <key> <value>`, `room unset <key>`, `room rule add|rm`, `room export`.
- [x] Briefing shows title/description/goal, rules, custom fields, policy overrides; digest shows changes.
- [x] `/team-sync:start` skill for the lead; `team-protocol` documents room config and rules.
- [x] Tests, DESIGN.md section, `schema/room-config.schema.json`, version 0.2.0.

## Later: distribution

Only needed when team-sync is used in repos other than this one.

- [x] Install via marketplace instead of `--plugin-dir` (verified 2026-10-29 with the SSH URL; v0.4.3 installs and is enabled):
      `claude plugin marketplace add ./` then `claude plugin install team-sync@tellet`.
- [ ] Copy `examples/project/claude-settings.json` to `.claude/settings.json` (with the real repo name)
      in a project repo so teammates get the plugin offered automatically.
- [x] Marketplace name and hosting: `tellet` (the hackathon is at tellet), hosted on GitLab (`git@gitlab.com:nikulinsanya/hackaton-tool.git`).

## v0.3: no dialogs, breaking-change warnings (done)

Decided (Sanya, 2026-10-26):
- **No approval dialogs for developers.** `overlap_action` default becomes `warn` (built-in, not just the
  hackathon room). `ask`/`deny` stay available as config.
- **Breaking changes are always surfaced.** When another agent announces a breaking change, my agent
  starts its next answer with a short warning (who changed what) plus a quick impact check (where this
  repo uses it), and proposes the adaptation without applying it.
- **No updates in the middle of a task.** PostToolUse no longer injects digests. Instead an
  **end-of-turn check** (Stop hook) runs once per turn: it surfaces breaking changes that arrived during
  the task, and, if this agent edited files, reminds it to announce anything breaking it introduced
  (`team change ... --breaking`) before it answers. Config `breaking_check` (default on) turns the
  self-check off.
- **Setup is run once, by the lead:** `/team-sync:start` per hackathon. Participants only launch their
  agents in the room. Running start again on a configured room shows it instead of re-creating it.
- The only remaining dialog: an agent trying to change room *policy* (guard against a prompt-injected
  agent switching checks off for everyone). `room create --from` asks only if the template has policy.

- [x] Default `overlap_action=warn`; examples, docs and tests follow.
- [x] PostToolUse: no digest; `digest_interval_sec` kept as a no-op for compatibility.
- [x] Stop hook: end-of-turn check (incoming breaking changes + own-edit announce reminder), once per
      turn, loop-safe (`stop_hook_active`); `breaking_check` config/policy key.
- [x] Prompt digest and briefing instruct: warn + impact check for each breaking change.
- [x] `pre-bash`: `--from` asks only when the template contains policy.
- [x] `/team-sync:start` detects an already configured room.
- [x] Tests, DESIGN.md, README, version 0.3.0.

## v0.4: announce what's ready (done)

Request (Sanya, 2026-10-26): a developer can report "Authorization is ready" so every other agent knows
and can start using the new API.

Design:
- `/team-sync:announce <what is ready>` (user-invoked skill). The agent turns the sentence into an
  actionable note for other agents: what is ready, how to use it (endpoint / function / module path,
  env vars), where the docs or tests are, and whether it is merged to main or only on a branch. If it
  added details beyond the developer's words, it shows the draft once before publishing.
- CLI `team announce "<text>"`: stored as a `changes` item with `kind: "ready"` (not breaking), so
  v0.3 clients still show it as a normal change. Auto-approved like other plain CLI calls.
- Receiving agents: the next prompt digest shows `* READY from <agent>: <text>` with an instruction to
  tell the developer in one line and, if the current task needs it, use it as described. Not part of
  the Stop hook (good news can wait for the next prompt).
- Briefing: "Available from the team:" lists recent announcements, so agents that start later know too.
- Protocol: when an agent finishes something others can use, it announces it (or suggests
  `/team-sync:announce`).

- [x] CLI `announce`, storage, digest + briefing lines, status output.
- [x] `/team-sync:announce` skill; team-protocol reaction + "announce when you finish" rule.
- [x] Tests, DESIGN.md, README, version 0.4.0.

## Milestone 2: hardening (good first hackathon tasks)

| # | Item | Done when |
|---|---|---|
| H1 | CI: `.gitlab-ci.yml` running `tests/` on python 3.8 and 3.12 (Linux; macOS runners on GitLab.com need a paid tier, so macOS stays a local check) | green pipeline |
| H2 | Per-agent file lock (`fcntl.flock` in `<common-dir>/team-sync/locks/<id>`) around load→save | `test_parallel_writers_same_machine` asserts all 12 items survive |
| H3 | `team gc`: squash branch history to one commit (keep tree), push with `--force-with-lease` | history < 10 commits after gc; other clones recover via the normal sync |
| H4 | Windows: call `python` / `py -3` when `python3` is missing (wrapper script or hook command fallback) | tests pass on windows-latest |
| H5 | Hook timing test: every hook < 300 ms with no network on a 50-agent room | test in suite |
| H6 | `doctor` checks: python/git versions, remote reachable, branch protection hint, clock skew | |

## Milestone 3: hackathon feature tracks

Each track is independent and owns its own files.

| Track | Idea | Main files |
|---|---|---|
| **A. Dashboard** | `team dashboard` generates one self-contained HTML page from the branch (rooms, agents, touching map, asks graph, timeline from git log). Optional `--serve` with auto refresh. | `scripts/dashboard.py`, new skill `dashboard` |
| **B. Task board** | Shared backlog per room: `rooms/<room>/tasks/<task-id>.json`, `team task add/list/claim/done`. Claim = write `owner` with compare-and-swap; on a merge conflict the lower `rev` loses and its agent is told. Briefing shows unclaimed tasks. | `team.py` (new subcommands), `team-protocol` |
| **C. Commit guard** | `team guard install` adds a git pre-commit hook that warns (or blocks) when the commit touches paths another online agent claimed. | `scripts/guard.py` |
| **D. Contract files** | `.team-sync.json` `"contracts": ["openapi.yaml", "src/types/**"]`: any edit there is auto-announced as a change, and other agents get it as a must-read in their digest. | `team.py` post-tool |
| **E. Smarter overlap** | Symbol-level: record changed function names from the edit (`old_string` in tool input) and match on symbol, not directory. Fewer false alarms. | `team.py` pre-edit/post-tool |
| **F. Notifications** | Optional desktop notification (macOS `osascript`) or Slack webhook when an ask is addressed to *your* agents, so humans see it without looking at the terminal. | `scripts/notify.py`, config key |
| **G. Protocol evals** | Scripted scenarios with `claude -p` in the sandbox: does the agent publish at task start, stop on overlap, answer asks? Score and tune `team-protocol/SKILL.md`. | `evals/` |

## Suggested split for 5 developers

| Dev | Morning | Afternoon |
|---|---|---|
| 1 | H1 CI, H2 lock | H3 gc, H6 doctor |
| 2 | A dashboard (static) | A live refresh + demo view |
| 3 | B task board (CLI + storage) | B in briefing/digest + protocol |
| 4 | C commit guard | D contract files |
| 5 | G evals (scenarios) | tune protocol wording from eval results, prepare demo |

**Dogfood it:** everyone's hackathon agents join room `hackathon` in *this* repo
(`CLAUDE_TEAM_ROOM=hackathon CLAUDE_TEAM_AGENT=<track> claude --plugin-dir plugins/team-sync`).
Agents not on the hackathon keep working normally. Optionally set `"auto_observe": "hackathon"`
so they still get collision warnings.

## Hackathon day outline

1. **Kickoff (30 min):** the lead runs `/team-sync:start hackathon --from examples/project/hackathon.json`
   once (edit title/goal first), demo of v0 in the sandbox, tracks, everyone joins the room.
2. **Build blocks** with a 5-min standup every 2 h, run from `team status --all`. Did agents report? Any overlap warnings?
3. **Freeze (1 h before end):** merge to main, `tests/` green.
4. **Demo (10 min):** the script below.

### Demo script

1. Three terminals side by side (sandbox or the real repo).
2. `api` agent: "refactor getUser to return null, it's breaking". It claims `src/auth/` and announces a breaking change.
3. `frontend` agent gets the breaking change in its next prompt and greps its usages unprompted.
4. `frontend` tries to edit `src/auth/login.ts`: the edit is held, Claude explains who is there and offers to ask.
5. `frontend` asks `api` for a fixture. `api` sees the ask, replies `--done`. `frontend` sees the reply.
6. `/team-sync:team` and the dashboard (track A) show the whole room.

## Open decisions (owner: Sanya)

- [x] Default for non-hackathon agents: **invisible** (no `auto_observe`). Decided 2026-10-26.
- [x] `overlap_action` during the hackathon: **warn**, set as room policy (`examples/project/hackathon.json`);
      the built-in default stays `ask` for other repos. In warn mode agents mention the overlap and keep going.
      Decided 2026-10-26.
- [x] Repo/marketplace name and where it is hosted: deferred to "Later: distribution".
- [x] Engine language: stay on Python (Node is not pre-installed anywhere and Claude Code's native
      installer doesn't need it; python3 ships with the Xcode CLT that git requires).
