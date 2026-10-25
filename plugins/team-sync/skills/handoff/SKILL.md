---
name: handoff
description: Write a handoff note to the team-sync room so another agent or person can continue this work.
argument-hint: [done]
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" *), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/team.py *), Bash(git status *), Bash(git log *), Bash(git diff *)
---

Write a handoff for this agent's current work.

1. Look at what was done in this session and at `git status` / recent commits on this branch.
2. Draft one compact paragraph (max ~350 characters): current state, what is left,
   gotchas, where to start. No secrets, no code blocks.
3. Show the draft to the user and ask for a quick OK.
4. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" update --handoff "<text>"`
   and, if the argument was `done` or the user says the task is finished, add `--status done`.
5. Release claims that are no longer needed: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" release --all`
   (only if the task is done).
