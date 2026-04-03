---
name: ecc-deep-research
description: Adapted from Everything Claude Code. Use when a task requires structured research across multiple sources, explicit source comparison, and a synthesized conclusion with citations, assumptions, and known gaps. Best for technical evaluation, vendor comparison, and product research.
---

# ECC Deep Research

Use this skill when the task needs more than one quick lookup.

## Research Flow

1. Frame the question
   - state the exact decision or fact to resolve
   - split broad questions into smaller answerable subquestions

2. Gather sources
   - prefer primary sources first
   - add secondary sources only when they help compare or explain
   - avoid relying on a single source for a non-trivial claim

3. Compare findings
   - note where sources agree
   - call out conflicts explicitly
   - separate facts from inference

4. Synthesize
   - answer the user’s actual decision
   - include constraints, tradeoffs, and unresolved gaps
   - keep recommendations tied to evidence

## Output

Summaries should include:

- direct answer
- key evidence
- source links
- assumptions
- unknowns or follow-up checks

Do not overstate certainty when evidence is mixed or incomplete.
