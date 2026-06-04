"""Phase 4a: repo enforcement test — every OutboxMessage(...) must set bot_key.

This test parses the source tree under ``src/sreda/`` with the stdlib ``ast``
module and FAILs if any ``OutboxMessage(...)`` constructor call does not pass
a ``bot_key=`` keyword argument.

Rationale (from second-tg-bot-final.md Phase 4):
  The column is nullable during the migration window, so the DB itself won't
  reject a missing bot_key.  This structural test is the enforcement gate that
  prevents new code from accidentally omitting bot_key and silently routing
  outbox rows through the wrong bot.

Opt-out: add ``# outbox-bot-key-enforcement: skip`` on the SAME LINE as the
``OutboxMessage(`` call if the construction genuinely cannot know the bot_key
(e.g. a pure-system path).  Document the reason in the comment.  The test
accepts this marker without counting it as a violation.

How it works:
1. Walk all .py files under src/sreda/.
2. Parse each file's AST.
3. For every ``ast.Call`` whose function name ends in ``OutboxMessage``, check
   whether the keyword arguments include ``bot_key``.
4. If not, check whether the source line carries the opt-out marker.
5. Any violation → FAIL with file:line details.
"""

from __future__ import annotations

import ast
import pathlib
from typing import NamedTuple

_SRC_ROOT = pathlib.Path(__file__).parent.parent.parent / "src" / "sreda"
_OPT_OUT_MARKER = "outbox-bot-key-enforcement: skip"


class Violation(NamedTuple):
    file: pathlib.Path
    lineno: int
    snippet: str


def _is_outbox_message_call(node: ast.Call) -> bool:
    """True when the call looks like ``OutboxMessage(...)``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "OutboxMessage"
    if isinstance(func, ast.Attribute):
        return func.attr == "OutboxMessage"
    return False


def _has_bot_key_kwarg(node: ast.Call) -> bool:
    return any(kw.arg == "bot_key" for kw in node.keywords)


def _collect_violations(src_root: pathlib.Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in sorted(src_root.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = source.splitlines()
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_outbox_message_call(node):
                continue
            if _has_bot_key_kwarg(node):
                continue
            # Check opt-out marker on the call's opening line (1-indexed).
            lineno = node.lineno
            line_text = lines[lineno - 1] if lineno <= len(lines) else ""
            if _OPT_OUT_MARKER in line_text:
                continue
            violations.append(Violation(
                file=path.relative_to(src_root.parent.parent),
                lineno=lineno,
                snippet=line_text.strip(),
            ))
    return violations


def test_every_outbox_message_sets_bot_key() -> None:
    """Fail if any OutboxMessage(...) construction omits bot_key=."""
    violations = _collect_violations(_SRC_ROOT)
    if not violations:
        return
    lines = [
        f"\n{len(violations)} OutboxMessage construction(s) missing bot_key=:\n"
    ]
    for v in violations:
        lines.append(f"  {v.file}:{v.lineno}  →  {v.snippet!r}")
    lines.append(
        "\nFix: add bot_key=<turn_bot_key> to each call, or add the comment"
        f" '# {_OPT_OUT_MARKER}' on the same line if bot_key is genuinely"
        " unavailable (document why)."
    )
    raise AssertionError("\n".join(lines))
