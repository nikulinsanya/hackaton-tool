# team-sync

Opt-in **rooms** where Claude Code agents share what they are doing, warn each other
before editing the same code, and pass decisions, breaking changes, learnings and
asks to each other. No server, no database: state lives on a git branch of the repo
you already use.

```
 Sanya's laptop                              Anna's laptop
 ┌────────────────────────────┐              ┌──────────────────────┐
 │ worktree A: agent frontend │──┐           │ agent api            │
 │ worktree B: agent tests    │──┤ same .git │                      │
 │ worktree C: agent (not in  │  │ = instant │                      │
 │   any room: untouched)     │  │           │                      │
 └────────────────────────────┘  │           └──────────┬───────────┘
            refs/heads/team-sync ┘                      │
                     │        git fetch/push            │
                     └──────────► origin ◄──────────────┘
                            branch team-sync:
                            rooms/hackathon/room.json
                            rooms/hackathon/agents/sanya-frontend.json
                            rooms/hackathon/agents/sanya-tests.json
                            rooms/hackathon/agents/anna-api.json
                            rooms/hackathon/config/anna-api.json  (title, rules, policy, ...)
```

- **Per-agent opt-in.** 5 developers, 15 agents, 7 in the `hackathon` room: only those 7 publish and react. The rest behave exactly as before (or, optionally, act as read-only *observers* so they still avoid collisions).
- **Automatic where it matters.** Hooks brief the agent at start, deliver room updates on every prompt and during long turns, record every edited file, and hold an edit for human approval when another active agent works in the same area.
- **Explicit where it matters.** The agent publishes intent (task, blocked, done, handoff), decisions, breaking changes, learnings and asks via a tiny CLI, following the `team-protocol` skill.
- **Safe.** Each agent writes only its own file. The team branch is never checked out. Your working tree and main branch are never touched. Entries are short and screened for obvious secrets. Hooks never break a session: any failure is a silent no-op.

## Repo layout

```
.claude-plugin/marketplace.json        marketplace "smartdodos" (this repo)
plugins/team-sync/
  .claude-plugin/plugin.json
  hooks/hooks.json                     SessionStart, UserPromptSubmit, Pre/PostToolUse, SessionEnd
  scripts/team.py                      the whole engine: CLI + hook handlers (stdlib Python 3.8+)
  skills/team-protocol/SKILL.md        when to write, how to react (model-invoked)
  skills/{join,leave,team,handoff,start}/  /team-sync:join, :leave, :team, :handoff, :start
  schema/agent.schema.json             status file schema
  schema/room-config.schema.json       room config file schema
examples/                              settings to drop into a project repo, a sample room template
tools/sandbox.sh                       local playground: origin + 2 clones + worktree
tests/test_team.py                     end-to-end tests (multi-clone, multi-worktree)
tests/test_sessions.py                 session simulator driven through hooks.json
docs/DESIGN.md                         architecture, data model, sync, reaction rules
PLAN.md                                status, roadmap, hackathon work split
CLAUDE.md                              guide for Claude Code working on this repo
```

## Try it in 2 minutes (no install)

```bash
python3 -m unittest discover -s tests          # 39 end-to-end tests
bash tools/sandbox.sh                          # prints 3 commands to start 3 agents
```

## Use it in a real project

1. The tool lives at `git@gitlab.com:nikulinsanya/hackaton-tool.git` (private; you need read access).
2. In the project repo, copy `examples/project/claude-settings.json` to `.claude/settings.json` and optionally `.team-sync.json`, and commit. Everyone who opens the project in Claude Code gets the plugin offered.
   Or per person: `claude plugin marketplace add https://gitlab.com/nikulinsanya/hackaton-tool.git && claude plugin install team-sync@smartdodos`.
3. Opt agents in:
   ```bash
   CLAUDE_TEAM_ROOM=hackathon CLAUDE_TEAM_AGENT=frontend claude
   # or inside a session:
   /team-sync:join hackathon --as frontend
   ```
4. See the room: `/team-sync:team`, or `python3 <plugin>/scripts/team.py status`.
5. A lead can publish the room's configuration (title, goal, rules, policy, custom
   fields) with `/team-sync:start`, or `team room create <room> --from hackathon.json`.

Requirements: git ≥ 2.20, python3 ≥ 3.8 (macOS: comes with the Xcode command line tools, which git needs anyway). Windows is untested.

## CLI

```
team join <room> [--as NAME] [--role member|observer]    team leave | whoami | doctor
team status [--all] [--json]                             team sync
team update [--status idle|in_progress|blocked|done] [--working-on T] [--blocked-on T] [--unblock] [--handoff T]
team learn T | decide T | change T [--breaking]
team ask <agent-id|all> T | reply <agent>#<ask> T [--done] | close <ask-id>
team claim <path>... [--note T] | release <path>... | release --all | check <path>...
team room [show] [--room R] [--json]                      team room --goal T          (legacy, still works)
team room create <room> [--title T] [--description T] [--goal T] [--from FILE]
team room set <key> <value> [--room R]  |  team room unset <key> [--room R]
team room rule add "<text>" [--room R]  |  team room rule rm <writer>#r<n> [--room R]
team room export [--room R]                               team init [--local|--remote R] [--auto-observe ROOM]
```

`team` = `python3 plugins/team-sync/scripts/team.py`. `<key>` is `title`, `description`, `goal`,
`policy.<name>` (`overlap_action`, `overlap_granularity`, `stale_after_min`, `touch_ttl_min`,
`digest_interval_sec`, `fetch_interval_sec` - changing these needs a human's approval), or any
custom field name. See `/team-sync:start` to bootstrap a room interactively.
