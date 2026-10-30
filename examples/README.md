# Examples

Optional files for a **project repo** whose agents coordinate (not this tool repo). None is needed:
the plugin is installed once per laptop (see the main README) and the project repo needs nothing.

| File | Goes to | Purpose |
|---|---|---|
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
