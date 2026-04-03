# AGENTS.md

## Session Bootstrap

- In a new session, always read:
  - `AGENTS.md`
  - `MEMORY.md`
  - `ERRORS.md`

## Global User Preferences

### Windows Text Editing

- For Russian-language `.md` files, always read and write with explicit `UTF-8` encoding.
- After editing text files that contain Cyrillic, immediately verify the result with explicit UTF-8 reading and `git diff` when available.
- If `apply_patch` or the sandbox editor fails, do not continue risky piecemeal edits blindly; switch to a safer editing path and verify the file after writing.
- Do not assume that an open file in VS Code is the cause of write issues unless there is clear evidence of an actual save conflict.
- For larger Markdown edits on Windows, preserve encoding first and reformat text second.
- Отвечай коротко и по делу.
- Перед тем как принять задачу в работу сделай глубокий вздох и приступай пошагово.

### PowerShell Safety

- Do not run complex one-line commands through `PowerShell` when they contain special shell syntax such as `>`, `<`, `>=`, `*`, `?`, `$()`, nested quotes, or heredoc-like constructs.
- For commands with complex quoting or embedded scripts, always prefer one of these safe paths:
  - `here-string`
  - piping a script into `python -`
  - a temporary script file
- Do not execute inline SQL inside a shell-quoted one-liner. Run SQL through a small Python script instead.
- Before running a remote command over `ssh`, check whether `PowerShell` will interpret any special characters locally.
- Do not run dependency installation and tests in parallel when tests depend on the package being installed.
- For multi-step remote verification, prefer sequential commands over parallel execution if one step depends on the previous step having completed successfully.

### Secrets Hygiene

- Local workspace secrets should live under ignored paths such as `.secrets/` and must never be committed.
- Do not place raw tokens, passwords, session dumps, or API keys in repo files, runtime helpers, docs, or tests.
- For local helper scripts, read credentials from ignored local storage instead of hardcoding them.
- Before committing, re-check tracked files for sensitive values if secrets handling was touched.

### Workspace Scope

- This workspace is for `Среда` and its related tooling.
- Do not mix files, docs, runtime paths, or terminology from the separate `Ассистент` / `OpenClaw` project back into this workspace.
- In `Среда` code and docs, prefer `.sreda/...` paths over legacy `.openclaw/...` paths unless explicitly documenting migration or compatibility.

### Spec-First Feature Development

- For new functionality, product changes, or feature work, always start with specs and documentation updates before writing code.
- Show the updated spec or design to the user and wait for explicit approval before implementation.
- Only after approval proceed to code changes, migrations, tests, and rollout steps.
- This rule applies by default in this workspace unless the user explicitly asks to skip it.
