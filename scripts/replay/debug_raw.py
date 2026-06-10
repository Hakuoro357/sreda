"""One-off debug: dump the RAW planner JSON for specific archive turns.

Imports run_replay (which configures env + builds the manifest/few-shot/
composer at import time), rebuilds the exact prompt for the chosen turns,
calls the planner once, and writes the raw model output to a utf-8 JSON
file so we can read exactly what the brain produced (no console mojibake).

NOT committed — local investigation only.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))

import run_replay as rr  # noqa: E402 — triggers env/manifest/composer setup
from sreda.runtime.planner.prompt_builder import build_prompt  # noqa: E402

TURNS = (11, 16, 20)

sample = json.loads(
    (rr._REPO_ROOT / "plans" / "replay-sample.json").read_text(encoding="utf-8")
)

out: list[dict] = []
for turn in TURNS:
    entry = sample[turn - 1]
    rec: dict = {"turn": turn, "user_message": entry["user_message"]}
    try:
        args = rr._build_prompt_args(entry)
        prompt = build_prompt(args)
        res = rr.call_planner(prompt, provider="mimo-v2.5-pro", attempt_no=1)
        rec["raw_text"] = res.raw_text
    except Exception as exc:  # noqa: BLE001 — debug capture
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["trace"] = traceback.format_exc()
    out.append(rec)
    print(f"captured turn {turn}")

dest = rr._REPO_ROOT / "plans" / "debug_raw_11_16_20.json"
dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"WROTE {dest}")
