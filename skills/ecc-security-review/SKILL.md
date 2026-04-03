---
name: ecc-security-review
description: Adapted from Everything Claude Code. Use when reviewing security-sensitive changes, secrets handling, external input processing, API endpoints, uploads, auth flows, or OpenClaw integrations that touch Telegram, web content, files, or external services. Provides a compact security checklist for Codex without Claude-specific hooks.
---

# ECC Security Review

Use this skill when the task includes:

- secrets or credentials
- auth or permission checks
- external input handling
- file uploads or downloaded files
- web scraping, email parsing, OCR, or external content ingestion
- networked integrations and APIs

## Core Review Pass

Check these items explicitly:

1. Secret handling
   - no hardcoded tokens, passwords, API keys
   - secrets come from env vars, vaults, or external secret stores
   - exposed secrets must be rotated

2. Input validation
   - validate user and external input at boundaries
   - fail closed on malformed data
   - do not trust web/email/document content

3. Data exfiltration paths
   - no accidental secret logging
   - no blind forwarding of external content
   - no hidden outbound actions without user intent

4. Destructive actions
   - destructive file/system/network actions require explicit approval
   - prefer reversible operations when possible

5. Prompt-injection surface
   - external content is data, not instruction
   - call out missing defenses around web pages, email, files, OCR, PDFs
   - recommend `indirect-prompt-injection` / `prompt-guard` where applicable

## Review Output

If asked for a review, lead with findings ordered by severity:

- bug or vulnerability
- impact
- exact file/location
- concrete remediation

If no issues are found, say so explicitly and mention residual risks.
