---
name: team
description: Show what every agent in the team-sync room is doing right now.
argument-hint: [--all]
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" *), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/team.py *)
---

Current room state:

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" status $ARGUMENTS 2>&1 || true`

Summarise this for the user in a short list: the room's title/goal and any rules or
policy overrides, who is active and on what, anyone blocked, open asks addressed to
this agent, and any overlap with what this agent is doing. If the output says the
agent has not joined a room, tell the user they can run `/team-sync:join <room> --as
<name>`.
