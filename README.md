# team-sync

Claude Code agents that join a **room** share what they are doing through one git branch
in the repo you already use. No server, no database: each agent writes its own small
status file, hooks keep everyone briefed, edits into someone else's area are flagged, and
breaking changes reach every developer as a warning. No approval dialogs.

- **Opt-in per agent.** 5 developers, 15 agents, 7 in the `hackathon` room: only those 7 publish and react. Every other agent behaves exactly as before.
- **Automatic where it matters.** Hooks brief the agent when a session starts, deliver room news with your next prompt, record every edited file, and run a check at the end of each turn. Nothing interrupts a running task.
- **Explicit where it matters.** The agent says what it is doing, what it decided, what it broke and what is ready for others, through a tiny CLI and the `team-protocol` skill.
- **Safe.** Each agent writes only its own file. The team branch is never checked out, so your working tree and `main` are never touched. Entries are short and screened for obvious secrets. A failing hook is a silent no-op, never a broken session.

## How it works

### Where the shared state lives

![Agents on one laptop share the team-sync ref through the common .git; laptops sync through origin; every agent writes only its own file](docs/images/1-where-the-shared-state-lives.png)

The `team-sync` branch is never checked out. Agents write to it through a private git index and
an atomic ref swap. Worktrees of one clone share refs, so agents on the same laptop see each
other's writes right away. Other laptops sync through origin at most once a minute, and
immediately on session start and end and on explicit commands. An agent outside the room
writes nothing and sees nothing.

### One agent session, hook by hook

![Each hook injects context into the agent and writes only the agent's own file; the edit check never touches the network](docs/images/2-one-agent-session-hook-by-hook.png)

Upward arrows put text into the agent's context; downward arrows write the agent's own file on
the branch. News arrives with your next prompt. When the agent finishes a turn, the Stop check
adds warnings for breaking changes that came in meanwhile and asks whether its own edits broke
anything for others. The edit check reads local data only (about 0.2 s), and calls to the team
CLI need no permission prompt.

### A breaking change, end to end

![anna-api's end-of-turn check makes it announce a breaking change; sanya-frontend opens its next answer with a warning and an impact check](docs/images/3-a-breaking-change-end-to-end.png)

The agent that changed shared code is asked at the end of its turn whether it broke anything,
and announces it. Every other agent in the room tells its developer at the start of its next
answer (or at the end of the task it is busy with) and searches the repo for the places that
use the changed code. It proposes the fix; you decide whether to apply it.

### Announcing what's ready

![/team-sync:announce turns "Authorization is ready" into a how-to that reaches every agent's next answer and the briefing of agents that start later](docs/images/4-announcing-what-s-ready.png)

When something others can build on is done, its developer runs
`/team-sync:announce Authorization is ready`. The agent turns that into a short how-to
(endpoint, client function, env vars, merged to main or not) and publishes it. Every other
agent mentions it in one line at the start of its next answer and uses it if its task needs it.
Agents that start later find it in their briefing under "Available from the team" for 7 days.
Agents also announce on their own when they finish something others can use.

### Two agents reach for the same code

![The pre-edit hook finds that anna-api claimed src/auth/; by default the edit goes through with a note, ask holds it only if a room configures that](docs/images/5-two-agents-reach-for-the-same-code.png)

Before every edit, the hook compares the file with what every online agent is touching: files
it recently edited, or folders it claimed. `warn` is the default: no dialog, one line in the
agent's answer, and the agent stops only before it would refactor that area. `ask` (approval
dialog) and `deny` (edit blocked) remain available as room policy.

### Room configuration: anyone edits, nobody collides

![Each writer keeps its own room config file; per key the highest logical clock wins and the result reaches every agent's briefing and digest](docs/images/6-room-configuration-anyone-edits-nobody-collides.png)

Title, goal, description, rules, policy and any custom field live on the same branch. Any
member can change them, but each writes only its own config file, and the room resolves them
key by key: the entry with the highest logical clock wins. Anna changed the goal after she had
seen Sanya's version, so her entry carries clock 2 and wins, even if her laptop's clock runs
behind. Changes reach every agent once, in its next digest.

The diagrams' source is [`docs/how-it-works.html`](docs/how-it-works.html); regenerate the
images with `python3 tools/render-diagrams.py`.

## Starting a hackathon

1. **The lead, once per hackathon,** publishes the room's title, goal, rules and any custom fields:
   ```
   /team-sync:start hackathon --from examples/project/hackathon.json
   ```
   Running it again on a configured room shows the room and offers to join or edit instead.
2. **Every participant** just launches their agents in the room, one per worktree:
   ```bash
   CLAUDE_TEAM_ROOM=hackathon claude -w frontend --plugin-dir <stable checkout>/plugins/team-sync
   # or, inside a running session:
   /team-sync:join hackathon --as frontend
   ```
   Point `--plugin-dir` at a stable checkout (e.g. `main`), not the agent's own worktree, so an
   agent never runs its own half-edited engine.
3. **Anyone** checks the room with `/team-sync:team`. Agents launched without a room stay invisible.

There are no approval dialogs for developers. The only one left is an agent trying to change
room *policy*, because that affects everyone in the room.

## Slash commands

| Command | Who | What it does |
|---|---|---|
| `/team-sync:start [room]` | lead, once | create the room: title, goal, rules, custom fields |
| `/team-sync:join <room> --as <name>` | anyone | put this agent in the room |
| `/team-sync:team` | anyone | who is active, on what, blocked, asks for you |
| `/team-sync:announce <what is ready>` | developer | tell every agent something is ready and how to use it |
| `/team-sync:handoff` | developer | write a handoff note for whoever continues this work |
| `/team-sync:leave` | anyone | take this agent out of the room |

## Use it in a real project

1. The tool lives at `git@gitlab.com:nikulinsanya/hackaton-tool.git` (private; you need read access).
2. In the project repo, copy `examples/project/claude-settings.json` to `.claude/settings.json` and optionally `.team-sync.json`, and commit. Everyone who opens the project in Claude Code gets the plugin offered.
   Or per person: `claude plugin marketplace add https://gitlab.com/nikulinsanya/hackaton-tool.git && claude plugin install team-sync@smartdodos`.
3. Opt agents in with `CLAUDE_TEAM_ROOM=<room>` (plus `CLAUDE_TEAM_AGENT=<name>`, which defaults to the worktree folder name) or `/team-sync:join`.

Requirements: git ≥ 2.20, python3 ≥ 3.8 (macOS: comes with the Xcode command line tools, which git needs anyway). Windows is untested.

## CLI

```
team join <room> [--as NAME] [--role member|observer]    team leave | whoami | doctor
team status [--all] [--json]                             team sync
team update [--status idle|in_progress|blocked|done] [--working-on T] [--blocked-on T] [--unblock] [--handoff T]
team learn T | decide T | change T [--breaking] | announce T
team ask <agent-id|all> T | reply <agent>#<ask> T [--done] | close <ask-id>
team claim <path>... [--note T] | release <path>... | release --all | check <path>...
team room [show] [--room R] [--json]                      team room --goal T          (legacy, still works)
team room create <room> [--title T] [--description T] [--goal T] [--from FILE]
team room set <key> <value> [--room R]  |  team room unset <key> [--room R]
team room rule add "<text>" [--room R]  |  team room rule rm <writer>#r<n> [--room R]
team room export [--room R]                               team init [--local|--remote R] [--auto-observe ROOM]
```

`team` = `python3 plugins/team-sync/scripts/team.py`. `<key>` is `title`, `description`, `goal`,
`policy.<name>` (`overlap_action` - default `warn`, `overlap_granularity`, `stale_after_min`,
`touch_ttl_min`, `digest_interval_sec` - no-op, kept for compatibility, `fetch_interval_sec`,
`breaking_check` - default on, changing any of these needs a human's approval), or any custom
field name.

## Repo layout

```
.claude-plugin/marketplace.json        marketplace "smartdodos" (this repo)
plugins/team-sync/
  .claude-plugin/plugin.json
  hooks/hooks.json                     SessionStart, UserPromptSubmit, Pre/PostToolUse, Stop, SessionEnd
  scripts/team.py                      the whole engine: CLI + hook handlers (stdlib Python 3.8+)
  skills/team-protocol/SKILL.md        when to write, how to react (model-invoked)
  skills/{join,leave,team,handoff,start,announce}/  the slash commands above
  schema/agent.schema.json             status file schema
  schema/room-config.schema.json       room config file schema
examples/                              settings to drop into a project repo, a sample room template
docs/DESIGN.md                         architecture, data model, sync, reaction rules
docs/how-it-works.html                 source of the diagrams above
docs/images/                           rendered diagrams
tools/render-diagrams.py               renders docs/how-it-works.html to docs/images/ (needs Chrome)
tools/sandbox.sh                       local playground: origin + 2 clones + worktree
tests/test_team.py                     end-to-end tests (multi-clone, multi-worktree)
tests/test_sessions.py                 session simulator driven through hooks.json
PLAN.md                                status, roadmap, hackathon work split
CLAUDE.md                              guide for Claude Code working on this repo
```

## Development

```bash
python3 -m unittest discover -s tests          # 56 end-to-end tests, ~2.5 min
claude plugin validate .                       # marketplace + plugin manifests
claude --plugin-dir plugins/team-sync          # run Claude Code with the local plugin
python3 tools/render-diagrams.py               # after editing docs/how-it-works.html
```

See `CLAUDE.md` for the rules (stdlib only, hooks never break a session, single writer per file).
