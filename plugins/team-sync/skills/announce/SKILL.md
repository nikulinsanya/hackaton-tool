---
name: announce
description: Tell the team-sync room that something is ready for other agents to use (an API, component, migration, shared util), with how to use it.
argument-hint: <what is ready>
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" *), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/team.py *), Bash(git log *), Bash(git diff *), Bash(git status *), Bash(git branch *)
---

Announce something ready so other agents in the room can start using it.

1. If `$ARGUMENTS` is empty, ask the user what is ready (one question).
2. Check it is actually committed and pushed somewhere other agents can fetch it:
   `git status` (uncommitted or untracked files that belong to the work, "ahead of
   origin/<branch>", "no upstream branch") and `git log origin/<branch>..HEAD` (empty
   output means nothing unpushed). If anything is uncommitted or unpushed, **do not
   announce yet**. Ask once: "Commit and push to main, then announce?" The team pushes
   finished work straight to `main` (`git add <files>`, `git commit -m "<message>"`,
   `git pull --rebase`, `git push`, one command per call); use a separate branch only if
   the user asks for one or the room rules say so. Run this only with their OK, then
   re-check before continuing.
3. Make it actionable for other agents: look at recent commits and the diff on this
   branch to find how to use it: endpoint(s) with method, exported function or module
   path, required env vars or config, where docs/tests are, and name exactly where it
   was pushed (`on main` or `on branch <name>`), since that is what another agent needs
   to fetch it.
4. Draft one announcement, max ~350 characters, like:
   `Authorization is ready: POST /auth/login {email,password} -> {token}; send
   Authorization: Bearer <token>. Client: src/auth/client.ts login(). Env:
   AUTH_SECRET. On main.`
   No secrets, no absolute paths.
5. If the draft only restates the user's words, publish right away. If you added
   details the user didn't write, show the draft and ask once ("Publish this?")
   before publishing.
6. Publish: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" announce "<text>"`.
   If what is ready also changes an existing interface callers use, additionally
   publish `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team.py" change "<what changed>" --breaking`.
7. Confirm to the user in one line what was published.

Run every command on its own, in the current directory: plain `git log`, `git diff`,
`git status`, `git branch` (no `git -C <path>`) and one `team.py` call per command (no
`&&`, `;`, pipes or redirects). Those are the forms that run without a permission prompt.
