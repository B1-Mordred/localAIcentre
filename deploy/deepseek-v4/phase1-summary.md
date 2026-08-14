# DeepSeek quality Phase-1A result

Phase-1A completed exactly 36 public B1 API runs. Phase-1B was not started.
Scores are independent Codex expert reviews on the requested ten 0-10
dimensions. Higher scores are better, including the dimensions named
`unsupported_assumptions`, `internal_contradictions`, and
`unnecessary_overengineering` (10 means none were present).

| Config | Budget / sampling | Complete | Mean | Median | Std dev | Runs with critical error | Mean completed runtime | Mean generated tokens |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| A | 384 + legacy | 9/9 | 7.278 | 8 | 2.200 | 4 | 132.1 s | 840 |
| B | 2048 + clean | 8/9 | 7.167 | 8 | 2.828 | 4 | 281.3 s | 1,828 |
| C | 4096 + clean | 8/9 | 7.500 | 9 | 2.749 | 3 | 352.1 s | 2,266 |
| D | 8192 + clean | 9/9 | 7.611 | 7 | 1.100 | 3 | 424.8 s | 2,744 |

The two incomplete outcomes, B/debug repetition 1 and C/debug repetition 3,
were managed safety terminations at the unchanged 88 C cutoff. They score zero
and were not rerun. A per-run cooling gate at the existing 65 C load threshold
was then used to remove accumulated-temperature ordering bias without changing
runtime settings. The D group therefore cannot be interpreted as intrinsically
more thermally stable merely because all three D responses completed.

## Decision

Keep `deepseek-quality` default at **4096** (`quality`). Compared with 2048,
4096 gained 0.333 mean points, raised the median from 8 to 9, reduced runs with
critical errors from four to three, and improved the difficult concurrency-test
mean from 6.50 to 7.83. The cost was 70.8 seconds / 25.2% more average completed
runtime and 438 / 24.0% more generated tokens.

Keep **8192 only as the opt-in `deep` level**, not as the default. Versus 4096,
it added just 0.111 mean points while average completed runtime increased by
72.7 seconds / 20.6% and generation increased by 478 tokens / 21.1%. Its median
fell from 9 to 7, concurrency quality fell from 7.83 to 6.50, and two debugging
answers introduced a self-audit error that lower budgets mostly avoided. The
small mean gain is not a meaningful quality improvement.

The 384 legacy condition was fast but volatile: one response leaked unfinished
deliberation, exceeded the 300-word constraint (819 words), and scored 2/10.

## Telemetry limitations and operational findings

- This pinned llama.cpp build streams separate `reasoning_content` but does not
  report reasoning-token counts in `usage`; those fields are recorded as null
  rather than estimated. Total completion tokens and visible/reasoning text are
  preserved for every complete run.
- GPU memory was 20,253 MiB loaded and 233 MiB unloaded. The 125 W configured
  power limit remained reported throughout. Temperature samples ranged 65-87 C.
- Host `MemAvailable` remained above 102 GiB. Swap was already almost completely
  occupied before testing; sampled `SwapFree` fell from 27,628 KiB to 5,972 KiB
  (minimum 2,396 KiB). Consequently the strict "no swap-out" acceptance check is
  **not claimed as passed**, even though no memory pressure was observed and the
  delta is only about 21 MiB.
- All raw answers, reasoning streams, usage, timings, scores, comments, and flags
  are in `benchmark-runs.jsonl`; `aggregate.json` is the machine-readable summary.

## Proposed Phase-1B (not run)

Retain only A (legacy control), B (2048), and C (4096). Run the larger category
set with randomized configuration order and a 65 C start gate for every run.
Drop D because it added no meaningful quality and materially increased latency.
Capture `/proc/vmstat` `pswpout` before and after each run in addition to
`SwapFree`, and keep thermal safety failures as terminal outcomes.
