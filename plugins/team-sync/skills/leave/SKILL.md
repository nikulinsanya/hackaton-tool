---
name: leave
description: Leave the team-sync room; this agent goes offline and stops sharing status.
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" *), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/team.py *)
---

1. If this agent has unfinished work, first write a handoff:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" update --handoff "<state, what is left, gotchas>"`
   (ask the user to confirm the text in one line).
2. Run: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" leave`
3. Confirm to the user in one sentence.
