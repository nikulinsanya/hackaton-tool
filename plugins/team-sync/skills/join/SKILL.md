---
name: join
description: Join a team-sync room with this agent so it shares status with the other agents in that room.
argument-hint: <room> [--as <agent-name>] [--role member|observer]
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" *), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/team.py *)
---

Join the team-sync room for this agent.

1. Run exactly: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" join $ARGUMENTS`
   - If no room name was given, ask the user for one (e.g. `hackathon`) and an agent
     name (default: this worktree's folder name) before running it.
2. Show the user the room overview from the output in a few lines: who is active and what they work on.
3. If the role is `member`, ask the user in one sentence what this agent will work on, then run
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" update --status in_progress --working-on "<their answer>"`.
4. From now on follow the `team-protocol` skill.
