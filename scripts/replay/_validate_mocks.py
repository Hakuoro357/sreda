"""One-shot validation: verify all mock outputs pass parse_tool_output()."""
import sys
sys.path.insert(0, "src")
sys.path.insert(0, "scripts/replay")

from mock_tools import _MOCK_RAW
from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.housewife import parse_tool_output, PARSERS

failures = []
skipped = []
ok_count = 0
for name, raw in _MOCK_RAW.items():
    if name not in PARSERS:
        skipped.append(f"  SKIP {name}: not in PARSERS")
        continue
    try:
        result = parse_tool_output(name, raw)
        if isinstance(result, ToolOutputContractViolation):
            failures.append(f"  VIOLATION {name!r}: raw={raw!r}")
        else:
            print(f"  OK {name}: {type(result).__name__}")
            ok_count += 1
    except Exception as e:
        failures.append(f"  ERROR {name!r}: {e}  raw={raw!r}")

if skipped:
    print("\nSkipped (no parser):")
    for s in skipped:
        print(s)

if failures:
    print("\nFAILURES:")
    for f in failures:
        print(f)
    sys.exit(1)
else:
    print(f"\nAll {ok_count} mocks with parsers OK (no violations)")
