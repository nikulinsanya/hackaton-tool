---
name: start
description: The lead names the room - create it (title, goal, description, rules, policy, fields), optionally from a saved template, and join it with this agent.
argument-hint: [room or idea name] [--from template.json]
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" *), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/team.py *)
---

The lead names the room. No room exists until this runs. It creates the room's
configuration (title, goal, description, rules, policy, custom fields) and joins this
agent to it, so every participant can then run `/team-sync:join <room>`.

**This is a one-time step for the lead.** Run it once per hackathon/project idea; every
participant then just runs `/team-sync:join` (or launches with `CLAUDE_TEAM_ROOM=<room>
claude ...`) - they should not run `/team-sync:start` again.

Run each `team.py` command on its own (no `&&`, `;`, pipes or redirects): that is what lets
it run without a permission prompt.

1. Parse `$ARGUMENTS`: an optional room or idea name and an optional `--from <file>`
   pointing at a template JSON file (the shape `room export` prints:
   `{"title", "description", "goal", "rules": [...], "policy": {...}, "fields": {...}}`).
   The user may type a free-text idea name (e.g. "Idea S"): slugify it (lowercase,
   `-`) for the room name, and if no title is given, use the text as typed as the title.
   **No name given:** run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" whoami`.
   - If this agent is already in a room, use that room.
   - Otherwise ask the user for the idea/room name (one question). Never fall back to a
     default silently.

2. **Check whether the room is already configured:**
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" room show --room <room> --json`.
   If `fields.title` or `fields.goal` is present in the result, the room has already
   been set up (each field's `by` tells you who set it, `at` when). Tell the user who
   set it up and when, show the current configuration (`room show --room <room>`), and
   offer to either (a) join the room now (`/team-sync:join <room>`), or (b) edit one
   field at a time with `room set <key> <value> --room <room>` / `room rule add|rm` -
   **do not** re-run `room create` on an already-configured room. Stop here in this case.

3. **If the room is not yet configured and a template path was given:** read it to
   show the user what will be applied, then skip to step 5.

   **Otherwise**, ask the lead for the following in one compact message (don't ask
   one question at a time): title (default: the idea name as typed), goal, description,
   any room rules everyone should follow, any policy overrides (`overlap_action`,
   `overlap_granularity`, `stale_after_min`, `touch_ttl_min`, `digest_interval_sec`,
   `fetch_interval_sec`, `breaking_check` - only mention these if asked or clearly
   relevant; the default `overlap_action` is already `warn`, no dialogs), and any extra
   custom fields (e.g. `track`, `demo_time`). Keep every value short (a sentence or less).

4. Show the user a short summary of what you are about to publish and ask for a
   quick OK before writing anything.

5. Run the commands:
   - `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" room create <room> --title "<title>" --description "<description>" --goal "<goal>" [--from <file>]`
   - For each rule: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" room rule add "<rule text>" --room <room>`
   - For each policy override (only on the human's explicit request - changing
     policy affects every agent and needs their approval anyway):
     `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" room set policy.<name> <value> --room <room>`
   - For each custom field: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" room set <key> "<value>" --room <room>`

   (Skip any of these the user didn't ask for; `room --from` already applies rules/
   policy/fields from the template in one write.)

6. Offer (in one line) to save the configuration as a template the team can commit. If they
   want it, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" room export --room <room>` and
   write its output to the file with your file-writing tool (no shell redirect: a `>` would
   need a permission prompt).

7. **Join this agent to the room you just created:**
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" join <room>` (add `--as <name>` only
   if the user gave a name for their own agent). If `whoami` showed this agent already in
   a *different* room before step 5, ask the user before switching it with this join. If
   the join output says `CLAUDE_TEAM_ROOM` in the environment overrides the room, tell the
   user that the repo or launch environment pins a room, and that they need to remove that
   `env` setting (in `.claude/settings.json` or the launch command) to actually be in the
   room just created.

8. Tell the user, in one or two lines, how others join: `/team-sync:join <room>` (or
   `/team-sync:join` with no name, to pick it from the rooms that exist).
