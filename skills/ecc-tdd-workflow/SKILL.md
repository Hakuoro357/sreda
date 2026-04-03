---
name: ecc-tdd-workflow
description: Adapted from Everything Claude Code. Use when implementing or fixing code where a disciplined test-first or test-tight workflow is needed. Best for bug fixes, regressions, parser logic, routing logic, and other changes where behavior should be pinned down before or during implementation.
---

# ECC TDD Workflow

Use this skill when behavior needs to be pinned down, not guessed.

## Workflow

1. Define expected behavior
   - identify the exact scenario to protect
   - write down what should happen and what should not

2. Add or choose a focused test
   - prefer the smallest test that proves the behavior
   - reproduce the current bug or missing behavior first when possible

3. Implement the change
   - make the minimum code change needed to satisfy the test
   - avoid opportunistic refactors unless they are required

4. Re-run targeted checks
   - confirm the new test passes
   - run nearby tests that cover adjacent behavior

5. Review the change
   - verify the implementation matches the intended behavior
   - look for regressions, brittle assertions, or overfitting to the test

## Pragmatic Rule

If the repo has no test harness for the touched area:

- say so explicitly
- use the smallest available executable check
- do not pretend TDD was fully applied

## Output

When summarizing work, include:

- behavior protected
- test added or reused
- code change made
- remaining test gaps
