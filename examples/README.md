# Examples

Copy these into the **project repo** whose agents should coordinate (not into this tool repo).

| File | Goes to | Purpose |
|---|---|---|
| `project/claude-settings.json` | `<project>/.claude/settings.json` (committed) | Everyone who opens the project is offered the marketplace and gets `team-sync` enabled. Points at `https://gitlab.com/nikulinsanya/hackaton-tool.git`; change the URL if you fork it. |
| `project/.team-sync.json` | `<project>/.team-sync.json` (committed) | Shared settings. All keys optional; these are the defaults. Set `"auto_observe": "hackathon"` to make every non-joined session in the repo an observer. |
| `worktree/claude-settings.local.json` | `<worktree>/.claude/settings.local.json` (not committed) | Opt one worktree's agent into a room without running `/team-sync:join`. |

Three ways to opt an agent in, pick one:

```bash
# 1. at launch
CLAUDE_TEAM_ROOM=hackathon CLAUDE_TEAM_AGENT=frontend claude

# 2. per worktree: copy claude-settings.local.json to <worktree>/.claude/settings.local.json

# 3. inside a running session
/team-sync:join hackathon --as frontend
```

Opt a session out even when `auto_observe` is set: `CLAUDE_TEAM_ROOM=off claude`.
