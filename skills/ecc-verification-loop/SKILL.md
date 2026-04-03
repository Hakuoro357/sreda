---
name: ecc-verification-loop
description: Adapted from Everything Claude Code. Use after meaningful changes to run a compact verification loop: build, typecheck, lint, tests, and diff review. Best for coding tasks where quality gates matter and you want a repeatable post-change routine without Claude-specific hooks.
---

# ECC Verification Loop

Use this skill after:

- implementing a feature
- fixing a bug
- refactoring code
- creating or editing scripts/skills/plugins

## Verification Order

Run the strongest applicable checks in this order:

1. Build
   - verify the project still builds if a build exists

2. Typecheck
   - run static type checks where available

3. Lint
   - run lint/format validation where available

4. Tests
   - run the most relevant tests first
   - prefer targeted tests before full-suite tests when the suite is expensive

5. Diff review
   - inspect what actually changed
   - look for stray debug code, dead code, accidental secret leaks, and risky behavior changes

## Reporting

Summarize verification with:

- what you ran
- what passed
- what failed
- what you could not run
- residual risk, if any

Do not claim full verification when checks were skipped or unavailable.

## Pragmatic Rule

If the repo has no build/lint/test setup, do not invent one. Report the gap and fall back to the strongest available checks.
