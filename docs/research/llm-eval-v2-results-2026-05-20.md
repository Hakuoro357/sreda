# llm_eval_v2 live model results, 2026-05-20

## Scope

This report summarizes the comparable `llm_eval_v2` live runs from 2026-05-20.

- Scenario set: 11 core scenarios.
- Runs: 2 runs per scenario, denominator 22.
- Mode: pre-full-loop one-call eval, unless noted otherwise.
- Source artifacts: `.tmp/llm_eval_v2_*.json`.
- Excluded from the comparison table: fake-provider checks, smoke-only runs, and the later 2026-05-21 fake `--full-loop` verification.

## Main Table

| Provider | Strict score | % | Avg latency | Total latency | Total cost | Cost / 1M tokens | Tokens | Reasoning tokens | unexpected_tool_sequence | final_state_mismatch | reply_expectation_failed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| openrouter:anthropic/claude-sonnet-4.6@default | 12/22 | 54.5% | 2.80s | 61.7s | $0.10890 | $4.12 | 26407 | 0 | 4 | 2 | 4 |
| openrouter:openai/gpt-5-mini@default | 11/22 | 50.0% | 9.90s | 217.9s | $0.02333 | $1.35 | 17330 | 8768 | 2 | 8 | 1 |
| mimo-v2.5-pro | 10/22 | 45.5% | 5.38s | 118.4s | $0.00000 | $0.00 | 21576 | 2501 | 5 | 7 | 0 |
| openrouter:google/gemma-4-26b-a4b-it@default | 10/22 | 45.5% | 2.33s | 51.4s | $0.00178 | $0.16 | 11295 | 0 | 4 | 8 | 0 |
| openrouter:minimax/minimax-m2.5@inceptron/fp8 | 10/22 | 45.5% | 5.27s | 115.9s | $0.00674 | $0.34 | 19594 | 10321 | 6 | 6 | 0 |
| openrouter:mistralai/mistral-nemo@default | 9/22 | 40.9% | 0.86s | 18.9s | $0.00116 | $0.09 | 13350 | 0 | 6 | 7 | 0 |
| openrouter:z-ai/glm-5@default | 9/22 | 40.9% | 6.03s | 132.7s | $0.01638 | $0.92 | 17866 | 4120 | 3 | 8 | 2 |
| openrouter:anthropic/claude-haiku-4.5@default | 8/22 | 36.4% | 2.10s | 46.2s | $0.03657 | $1.38 | 26441 | 0 | 8 | 6 | 0 |
| openrouter:deepseek/deepseek-v4-flash@deepseek | 8/22 | 36.4% | 3.17s | 69.8s | $0.00236 | $0.11 | 21704 | 3880 | 5 | 7 | 2 |
| openrouter:google/gemini-2.5-flash-lite@default | 8/22 | 36.4% | 0.51s | 11.1s | $0.00082 | $0.13 | 6313 | 0 | 6 | 8 | 0 |
| openrouter:openai/gpt-5.4-nano@default | 8/22 | 36.4% | 1.26s | 27.8s | $0.00263 | $0.35 | 7550 | 0 | 8 | 6 | 0 |
| openrouter:qwen/qwen3.6-plus@default | 8/22 | 36.4% | 19.70s | 433.3s | $0.04864 | $1.26 | 38674 | 20512 | 6 | 8 | 0 |
| openrouter:stepfun/step-3.5-flash@stepfun/fp8 | 8/22 | 36.4% | 1.97s | 43.3s | $0.00278 | $0.13 | 21013 | 0 | 5 | 7 | 2 |
| openrouter:tencent/hy3-preview@siliconflow | 8/22 | 36.4% | 9.95s | 219.0s | $0.00605 | $0.17 | 35160 | 19052 | 3 | 8 | 3 |
| openrouter:x-ai/grok-4.3@default | 8/22 | 36.4% | 12.00s | 264.0s | $0.04006 | $1.53 | 26221 | 10608 | 6 | 8 | 0 |
| openrouter:minimax/minimax-m2.7@sambanova | 7/22 | 31.8% | 2.57s | 56.5s | $0.01944 | $1.07 | 18097 | 3299 | 8 | 7 | 0 |

## Interpretation

The strict score is intentionally harsh. A large share of failures are `final_state_mismatch`, and many of those are comparator strictness rather than clear model failure:

- lowercased reminder titles such as `поймать ежика` vs expected `Поймать ежика`;
- correct local time without explicit `+03:00`;
- small formatting differences in otherwise usable tool arguments.

These strictness failures are useful for harness hardening, but they should not be treated as equal to planning failures.

`unexpected_tool_sequence` is different. These are real behavior failures:

- missing a required tool call after an action request;
- calling `schedule_reminder` again when the expected second turn is `update_reminder`;
- using list/read tools when the scenario requires a write tool, or vice versa;
- doing only part of a multi-action request.

Those errors should remain hard failures.

## Cost Notes

OpenRouter cost is taken from the eval artifacts. For direct MiMo pricing, Boris stated the current effective price is 16 USD per 200,000,000 tokens, or about $0.08 per 1M tokens. At that direct rate, the `mimo-v2.5-pro` 21,576-token run would be about $0.0017, not `$0.00`; the table shows `$0.00` because the local artifact did not record direct MiMo billing.

## Recommendation

No tested replacement beats MiMo on the combined price/quality/risk profile.

- Keep MiMo as the production baseline for now.
- Do not change production routing based on these evals alone.
- Use Claude Sonnet / GPT-5-mini / Gemma only as research or shadow candidates, not production defaults: Sonnet is best strict score but expensive, GPT-5-mini is slower and still not clean, Gemma is cheap but not materially better than MiMo.
- Improve `llm_eval_v2` comparator before using strict score as a final vendor-selection metric.

## Follow-Up

Implemented on 2026-05-21: explicit `--full-loop` mode for production-parity eval shape:

`tool_call -> stub tool result -> final assistant reply`

This keeps default behavior unchanged and lets future live evals measure final user-visible answers after tool results.
