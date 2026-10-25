---
name: start
description: Bootstrap a team-sync room's configuration (title, goal, description, rules, policy, fields) for the lead kicking off a hackathon or project, optionally from a saved template.
argument-hint: [room] [--from template.json]
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" *), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/team.py *)
---

Set up a room's configuration for the team. This does not join the room; it just
publishes the room's title, goal, description, rules, policy and any custom fields
so everyone who joins afterwards gets them in their briefing.

1. Parse `$ARGUMENTS`: an optional room name (default `hackathon`) and an optional
   `--from <file>` pointing at a template JSON file (the shape `room export` prints:
   `{"title", "description", "goal", "rules": [...], "policy": {...}, "fields": {...}}`).

2. **If a template path was given:** read it to show the user what will be applied,
   then skip to step 4.

   **Otherwise**, ask the lead for the following in one compact message (don't ask
   one question at a time): title, goal, description, any room rules everyone should
   follow, any policy overrides (`overlap_action`, `overlap_granularity`,
   `stale_after_min`, `touch_ttl_min`, `digest_interval_sec`, `fetch_interval_sec` -
   only mention these if asked or clearly relevant), and any extra custom fields
   (e.g. `track`, `demo_time`). Keep every value short (a sentence or less).

3. Show the user a short summary of what you are about to publish and ask for a
   quick OK before writing anything.

4. Run the commands:
   - `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" room create <room> --title "<title>" --description "<description>" --goal "<goal>" [--from <file>]`
   - For each rule: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" room rule add "<rule text>" --room <room>`
   - For each policy override (only on the human's explicit request - changing
     policy affects every agent and needs their approval anyway):
     `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" room set policy.<name> <value> --room <room>`
   - For each custom field: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" room set <key> "<value>" --room <room>`

   (Skip any of these the user didn't ask for; `room --from` already applies rules/
   policy/fields from the template in one write.)

5. Offer to save the resulting configuration as a template the team can commit:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" room export --room <room> > <file>`.

6. Offer to join the room now: `/team-sync:join <room> --as <name>`.
