# CLAUDE.md: working on team-sync

This repo is a Claude Code plugin marketplace with one plugin, `team-sync`, which lets
Claude Code agents share status through a git branch. Read `docs/DESIGN.md` for the
architecture and `PLAN.md` for what to do next. Start with PLAN.md Milestone 1.

## Map

- `plugins/team-sync/scripts/team.py`: the whole engine. CLI + hook handlers. One file on purpose.
- `plugins/team-sync/hooks/hooks.json`: wires hook events to `team.py hook <event>`.
- `plugins/team-sync/skills/team-protocol/SKILL.md`: what agents publish and how they react. Behaviour changes usually belong here, not in code.
- `plugins/team-sync/skills/{join,leave,team,handoff,start,announce}`: user slash commands.
- `tests/test_team.py`: end-to-end tests with real git repos in a temp dir.
- `tests/test_sessions.py`: session simulator driven through `hooks.json`, like a real Claude Code session.
- `tools/sandbox.sh`: manual playground for real Claude Code sessions.

## Commands

```bash
python3 -m unittest discover -s tests -v       # all tests (~2 min)
python3 -m unittest tests.test_team.TeamSyncTests.test_overlap_ask_once_then_quiet
python3 -m unittest discover -s tests -v -k SessionTests     # just the session-sim suite (~1 min)
claude plugin validate .                         # marketplace + plugin manifests
bash tools/sandbox.sh /tmp/ts-sandbox            # playground, prints launch commands
claude --plugin-dir plugins/team-sync            # run Claude Code with the local plugin
```

## Rules

1. **Stdlib only, Python 3.8+.** No pip dependencies, no walrus/match/`X | Y` types. macOS ships 3.9.
2. **Hooks must never break a session.** Every hook path exits 0. Errors go to `<git-common-dir>/team-sync/error.log`. Not joined → no output, no writes.
3. **Hooks must be fast.** No network in `pre-edit`. Network only through `Store.sync()`, which is throttled.
4. **Single writer per file.** An agent writes only `rooms/<room>/agents/<its-id>.json`, and only its own `rooms/<room>/config/<writer-id>.json` for room config (which mirrors `goal` into the legacy `room.json` on every write). New shared data needs its own per-writer files, or a compare-and-swap design.
5. **Never touch the user's working tree, index or current branch.** Write the team branch only through `Store.write()` (temp index + `update-ref` CAS).
6. **No absolute paths, hostnames or secrets in shared files.** Paths are repo-relative. All free text goes through `clean_text()`.
7. **Schema changes stay backwards compatible.** Add fields with defaults in `new_doc()`, never rename. Bump `SCHEMA` and update `schema/agent.schema.json` for anything else.
8. Every bug fix or feature gets an end-to-end test in `tests/test_team.py`, using the `Env` helper.
9. Bump `version` in `plugins/team-sync/.claude-plugin/plugin.json` and `VERSION` in `team.py` together.

## Definition of done

Tests green, `claude plugin validate .` passes, the PLAN.md item is checked off, and
`docs/DESIGN.md` is updated if behaviour or storage changed.
