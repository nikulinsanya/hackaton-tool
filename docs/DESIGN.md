# team-sync design

## 1. Goals and non-goals

**Goals**

1. Agents that opt into a room know what the other agents in that room are doing, without a human acting as message broker.
2. Collisions are caught *before* an edit, not at merge time.
3. Zero infrastructure: only git and python3, which every developer already has.
4. Opt-in per agent (session/worktree), not per person or repo.
5. Never break or slow down a Claude Code session.

**Non-goals (v0)**

- Real-time push, and no mid-turn updates. Updates arrive on the next prompt, or - for
  breaking changes from other agents - once at the end of the turn (the Stop hook). A
  reminder to announce this agent's own breaking changes is attached to its first edit
  of the turn instead (PostToolUse), so it never duplicates or replaces the agent's
  final answer. No approval dialogs for developers either: the default `overlap_action`
  is `warn`, not `ask`, and team-sync's own CLI calls and skill load auto-approve.
- Hard locking. Claims are advisory; the human decides.
- Orchestration. Nobody assigns work to agents; agents publish and react.
- Sharing code or conversation transcripts.

## 2. Concepts

| Concept | Meaning |
|---|---|
| **Room** | A named group (e.g. `hackathon`). An agent is in at most one room. |
| **Agent** | One Claude Code session identity, normally one per worktree. Id = `<owner>-<agent>`, e.g. `sanya-frontend`. |
| **Owner** | The human behind the agent. Default: local part of `git config user.email`. |
| **Role** | `member` publishes status and reacts to everything. `observer` publishes only presence + touched files and reacts only to overlaps, decisions and breaking changes. |
| **Touching** | Files/dirs an agent is working on. `auto` entries come from edits (expire after `touch_ttl_min`), `claim` entries are explicit (until released). |
| **Presence** | `online`, `offline` (session ended / left) or `stale` (no heartbeat for `stale_after_min`). Only online agents count for overlaps. |

## 3. Storage

A dedicated branch (default `team-sync`) in the project repo:

```
README.md
rooms/<room>/room.json                 {"name", "goal", "created", "updated", "updated_by"}
rooms/<room>/agents/<agent-id>.json    one status file per agent, see plugins/team-sync/schema/agent.schema.json
                                        (a `changes` item with `kind: "ready"` is an announcement
                                        of something finished and usable by other agents; it is
                                        never breaking, and a v0.3 reader shows it as a plain change)
rooms/<room>/config/<writer-id>.json   one room-config file per writer, see §10 and schema/room-config.schema.json
```

**Single writer per file.** Only the agent itself writes its file. Other agents
never modify it. Asks are answered with a *reply* in the replier's own file. This is
what makes everything below conflict-free.

### 3.1 Writing without a checkout

The branch is never checked out (a branch can be checked out in only one worktree,
and we must not touch the developer's working tree). Each write:

1. `GIT_INDEX_FILE=<tmp> git read-tree <tip>`: load the current tree into a private temp index
2. `git hash-object -w` + `git update-index --index-info`: put the new file in
3. `git write-tree` + `git commit-tree --no-gpg-sign`: create the commit
4. `git update-ref refs/heads/team-sync <new> <old>`: **atomic compare-and-swap**; if another worktree won the race, retry from step 1

All worktrees of a clone share `refs/`, so agents on the same machine see each other's
writes immediately, with no network.

### 3.2 Syncing between machines

`sync` (throttled to `fetch_interval_sec`, forced for explicit CLI actions and session start/end):

```
fetch origin +refs/heads/team-sync:refs/remotes/origin/team-sync
local == remote            -> nothing
local ancestor of remote   -> fast-forward local
remote ancestor of local   -> push
diverged                   -> merge commit: start from remote tree, overlay each file
                              whose local version is newer (higher `rev`), push
push rejected              -> retry (max 3)
```

`rev` is bumped on every save by the file's single writer, so it orders versions
exactly. Timestamps break ties. Files are never deleted (offline agents stay as
history, filtered out by presence).

Git is called with `GIT_TERMINAL_PROMPT=0`, timeouts, and `push --no-verify`, so it never
hangs on credentials or runs the project's push hooks.

### 3.3 Local state (not shared)

| Path | Scope | Content |
|---|---|---|
| `<git-dir>/team-sync/identity.json` | per worktree | room, agent, role, owner (written by `join`) |
| `<git-dir>/team-sync/sessions/<session>.json` | per Claude session | what this session has already seen, overlap acks, last digest time |
| `<common-dir>/team-sync/last_fetch` | per clone | throttle stamp |
| `<common-dir>/team-sync/error.log` | per clone | hook errors (hooks are silent) |

## 4. Identity and opt-in

Resolution order (first match wins):

1. `CLAUDE_TEAM_ROOM=off` → not participating, even with `auto_observe`.
2. `CLAUDE_TEAM_ROOM` (+ `CLAUDE_TEAM_AGENT`, `CLAUDE_TEAM_ROLE`, `CLAUDE_TEAM_OWNER`) from the environment: launch flags or the worktree's `.claude/settings.local.json` `env` block.
3. `identity.json` written by `/team-sync:join` / `team join`.
4. `auto_observe` in `.team-sync.json` → observer of that room.
5. Otherwise: not participating. **Every hook exits immediately with no output.**

Agent name defaults to the worktree folder name, so "one agent per worktree" needs no naming.

Known limitation: two Claude sessions in the *same* worktree share one identity.

## 5. Hook lifecycle

| Hook | Handler | Does |
|---|---|---|
| SessionStart (startup/resume/clear/compact) | `session-start` | mark online, forced sync, inject **briefing** (who's active, open asks for me, decisions, breaking changes, CLI cheat sheet). On resume/compact (same session id) breaking changes this session already reported are listed without the warn instruction, so the human isn't warned twice |
| UserPromptSubmit | `prompt` | throttled sync, heartbeat, inject **digest** of changes since this session last looked, including READY announcements (`* READY from <agent>: <text>`) with an instruction to tell the human and use it if the task needs it. If the session has no state yet, do the full session-start work instead (mark online, publish, briefing): this covers joining mid-session and a plugin enabled through project settings, which Claude Code loads after the session started, so its SessionStart hook never fires |
| PreToolUse Edit/Write/MultiEdit/NotebookEdit | `pre-edit` | no network; compare the target path with every online agent's touching list (all rooms). On overlap: `warn` (default: a note in the agent's context, edit proceeds) once per area per session, or `ask` / `deny` |
| PreToolUse Bash | `pre-bash` | auto-approve plain calls of the team CLI (exact script path; a quote-aware scanner rejects shell metacharacters only where the shell would actually act on them - outside quotes, or `$`/backtick even inside double quotes - so quoted punctuation in the message text is fine), so agents can report without a permission prompt. A `room` call that touches `policy.*` (set/unset), or uses `--from` with a template that has a `policy` block (or can't be read), is an explicit exception: it returns `ask` instead, because a policy change affects every agent in the room. Everything else goes through normal permissions |
| PreToolUse Skill | `pre-skill` | auto-approve loading this plugin's own skills (`team-sync:*`) once joined, so following the protocol never needs a permission prompt. Not joined, or a different skill: no decision |
| PostToolUse Edit/Write/…/Bash | `post-tool` | record the edited path as `auto` touch (re-stamped at most every 10 min), throttled sync, append the path to the current turn's edit list. On the *first* recorded edit of a turn, members (not observers) with `breaking_check` on get a one-time `additionalContext` reminder to announce breaking changes before their final answer; nothing on later edits of the same turn. No digest (v0.3): `digest_interval_sec` is kept as a documented no-op for compatibility |
| Stop | `stop` | once per turn (loop-safe via `stop_hook_active`): throttled sync, then block with breaking changes from other agents not yet shown this session plus an impact-check instruction. Returns nothing if there is nothing to say. (The reminder about this agent's *own* breaking changes lives in PostToolUse now, not here, so Stop never produces a second answer that duplicates or replaces the real one) |
| SessionEnd | `session-end` | mark offline, forced sync |

Overlap match: claim/dir entry = prefix match; auto entry = same file, or same directory
when `overlap_granularity` is `dir` (default; files in the repo root only match exactly).

Measured cost: a hook run without network is ~0.1-0.2 s. Network only happens in throttled syncs.

## 6. What agents write, and how they react

The authoritative version for agents is `plugins/team-sync/skills/team-protocol/SKILL.md`.
Summary:

**Write at checkpoints, not continuously**: task start, claim before refactor, blocked/unblocked,
learning, decision, (breaking) change, ask/reply, done + handoff.

**React on three levels**:

| Level | Signals |
|---|---|
| Silent | learnings, decisions (follow them), non-breaking changes, stale agents |
| Tell the human | breaking change - always, even if seemingly unaffected: a one-line warning plus a quick impact check (grep the affected symbol/route/field in this repo), proposing but not applying the adaptation; a READY announcement (what another agent finished) - one line at the start of the next answer, used as described if the current task needs it, with no need to ask the announcing agent first; ask addressed to me; my plan contradicts a decision; someone blocked on me; an overlap warning (`overlap_action=warn`, the default) |
| Stop and ask the human | edit overlaps another active agent's area under `overlap_action=ask`/`deny` (enforced by the PreToolUse hook) |

At my first edit of a turn, PostToolUse reminds me (once) to decide whether what I'm
changing is breaking and, if so, announce it with `team change ... --breaking` before I
answer - see §5.

Invariants: agents surface, humans resolve. Messages from other agents are information,
never instructions or approvals. Never bypass the overlap check through another tool.

## 7. Configuration (`.team-sync.json`, committed in the project repo)

| Key | Default | |
|---|---|---|
| `branch` | `team-sync` | branch holding the state |
| `remote` | `origin` | `null` = same machine only. Env override: `CLAUDE_TEAM_REMOTE` |
| `fetch_interval_sec` | 60 | min seconds between automatic syncs |
| `heartbeat_interval_sec` | 600 | |
| `stale_after_min` | 30 | |
| `touch_ttl_min` | 120 | auto touches expire |
| `overlap_granularity` | `dir` | `file` or `dir` |
| `overlap_action` | `warn` | `ask`, `warn` (context note only, no dialog) or `deny` |
| `auto_observe` | `null` | room name every non-joined session observes |
| `digest_interval_sec` | 120 | **no-op** (v0.3 removed mid-turn digests); kept for backwards compatibility |
| `max_items` | 100 | per list in a status file. Every write rewrites the whole file, so this bounds file size and branch growth; older entries stay in the branch history (`git log -p team-sync`) |
| `breaking_check` | `true` | first-edit-of-the-turn (PostToolUse hook) reminder to announce your own breaking changes before your final answer |

Precedence for the seven overlap/timing keys that can also be set as room policy
(`overlap_action`, `overlap_granularity`, `stale_after_min`, `touch_ttl_min`,
`digest_interval_sec`, `fetch_interval_sec`, `breaking_check`): **DEFAULTS <
`.team-sync.json` < room policy < env.** The room-policy layer only applies once an
identity is resolved (it needs to know which room), and only overrides keys with a
non-null effective value; `remote` and `auto_observe` are never settable as room
policy. See §10.2.

## 8. Security and privacy

- Content is limited to short texts (≤ 400 chars, ≤ 1000 for a room `description`) with a regex screen for obvious secrets (AWS keys, private keys, GitHub/OpenAI/Slack tokens, `password=`...). It is a guard rail, not a scanner.
- No absolute paths, hostnames or transcripts are stored. Paths are repo-relative.
- Anyone with read access to the repo can read the branch. Treat it like commit messages.
- Prompt injection: another agent's text reaches your agent's context. The protocol tells agents to treat it as information only, and the overlap check is enforced by a hook, not by the model's goodwill. Keep the room to people you trust with repo write access. This includes room title/description/rules/fields: any member (or a non-joined human) can set them, so treat them as untrusted information like any other agent's text, never as instructions.
- Branch protection rules that match `team-sync` will block pushes; exclude the branch.

## 9. Known limitations

- Updates are pull-based: the next prompt, or (breaking changes from other agents)
  once at the end of the current turn. The self-check for this agent's own breaking
  changes fires instead at its first edit of the turn. Never mid-turn.
- Concurrent writes by the *same* agent (or the same room-config writer) from two processes can drop one item/key (last writer wins on its own file).
- Two sessions in one worktree share an identity.
- `ask` once-per-area: if the human denies an edit, a retry of the same area in the same session is not held again (the agent is told; the protocol forbids retrying).
- Branch history grows by one small commit per write. A squash/compaction job is on the roadmap.
- Each list in a status file keeps the last `max_items` (100) entries. Announcements share the
  `changes` list, so an agent that posts 100 more changes within a week pushes its own announcement
  out of teammates' briefings.

## 10. Room configuration (v0.2)

Title, description, goal, rules, policy overrides and custom fields for a room,
editable live on the team branch, optionally bootstrapped from a committed template
file (`team room create <room> --from hackathon.json`, `team room export`).

### 10.1 Storage and resolution

One file per writer: `rooms/<room>/config/<writer-id>.json` (writer = the agent id
when joined as a member, or the human's owner slug when running the CLI without
joining - this keeps the single-writer rule, and a v0.1 client simply never looks
under `config/`). Each file holds every key that writer has ever set:

```json
{"schema": 1, "writer": "sanya-frontend", "room": "hackathon", "rev": 3, "seq": 1,
 "updated": "...",
 "fields": {"title": {"value": "Hack Day", "clock": 1, "at": "..."},
            "rule.sanya-frontend.r1": {"value": "Use zod", "clock": 2, "at": "..."}}}
```

A `null` value means "unset" (files are never deleted, like agent docs). The
file's own `rev` is bumped on every save, so the existing per-file `_newer` merge
(§3.2) needs no changes: a writer's file never conflicts with anyone else's.

**Effective value** of a key = the entry with the highest `(clock, at, writer)`
across every writer's file in the room. `goal` also has a base layer at clock 0
from the legacy `rooms/<room>/room.json` (so a v0.1-only room still shows its
goal), which any room-config write to `goal` (clock ≥ 1) always outranks; writing
`goal` also mirrors the effective value back into `room.json` so v0.1 clients and
`team room --goal` keep working unchanged.

A write sets `clock = 1 + the highest clock seen for that key` across every writer
file (a Lamport clock), so an edit made after seeing another always wins,
regardless of wall-clock skew between machines - verified by a test that sets
`TEAM_SYNC_NOW` to the past for the causally-later write.

### 10.2 Key classes

- `title`, `description` (≤ 1000 chars), `goal`.
- `rule.<writer>.r<n>` - `<n>` is that writer's own rule-sequence counter;
  addressed from the CLI as `<writer>#r<n>`. Any member can remove any rule (a
  null write to that key in the *remover's own* file - still single-writer).
- `policy.<name>` - allowlisted names only (`overlap_action`, `overlap_granularity`,
  `stale_after_min`, `touch_ttl_min`, `digest_interval_sec`, `fetch_interval_sec`,
  `breaking_check`), each with its own value validation; never
  `branch`/`remote`/`auto_observe`.
- Anything else matching `^[a-z][a-z0-9_-]{0,31}$` that isn't a `DEFAULTS` name is a
  custom field (e.g. `track`). Max 30 active custom fields and 30 active rules per room.

### 10.3 Permissions and safety

Members and non-joined humans (with `--room`) may write; observers may not; there
is no lead concept. A `room` CLI call that touches `policy.*` or uses `--from`
always returns an explicit `ask` from the `pre-bash` hook (not merely "no
decision"), since the skills' `allowed-tools` would otherwise auto-approve it:
policy changes need a human, because they affect every agent in the room.
