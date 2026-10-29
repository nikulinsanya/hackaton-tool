---
name: join
description: Join a team-sync room with this agent so it shares status with the other agents in that room.
argument-hint: [room] [--as <agent-name>] [--role member|observer]
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" *), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/team.py *)
---

Join the team-sync room for this agent.

1. **A room name was given in `$ARGUMENTS`:** run exactly
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" join $ARGUMENTS`.

   **No room name was given:** run
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" rooms` and show the user the rooms
   briefly (title/goal, who's online).
   - No rooms at all: tell the user a lead must create one first with
     `/team-sync:start <name>`, and stop here.
   - Exactly one room: propose joining it.
   - More than one: ask which to join.
   In the same question, ask for an agent name too (default: this worktree's folder
   name) - suggest a distinct name if two laptops belonging to the same person would
   otherwise collide on it (e.g. `sanya-laptop2`). Then run
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" join <room> --as <name>`.

2. Show the user the room overview from the output in a few lines: who is active and what they work on.
3. If the role is `member`, ask the user in one sentence what this agent will work on, then run
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" update --status in_progress --working-on "<their answer>"`.
4. From now on follow the `team-protocol` skill.
