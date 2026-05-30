# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`MAS_trajectory_analysis` is a 4-stage LLM pipeline that takes a 500–800 step multi-agent **failure** trajectory and produces a lean, display-oriented JSON that is attached to an annotation platform: a task goal, a phase timeline (drill into sub-steps + anomaly signals), and a single root-cause suggestion with a failure chain, evidence, and confidence. Every `step_id` is forced back to the real trajectory to prevent the LLM from fabricating references.

It is one subproject of the larger `MASFA` repo (git root is `…/MASFA`, which also vendors `OpenHands/` and `harbor/` — each with its own setup; ignore them when working here). The README in this directory is authoritative and detailed (in Chinese) — read it for the full input/output field contract and taxonomy rationale.

## Commands

This is a Python package named `MAS_trajectory_analysis` living at `MAS_trajectory_annotate/scripts/MAS_trajectory_analysis`. Run it as a module **from the `scripts/` parent dir**, or run `run.py` directly (it injects `sys.path` as a fallback).

```bash
pip install -r requirements.txt          # only dep: openai>=1.0
export LLM_API_KEY=sk-...                 # required to call the LLM (DEEPSEEK_API_KEY also accepted)
# optional: export LLM_BASE_URL / LLM_MODEL to point at any OpenAI-compatible endpoint (default DeepSeek)

# classic layout: <input-dir>/<benchmark>/*.json
cd ../  &&  python -m MAS_trajectory_analysis.run --benchmark swe_bench_pro --workers 4

# flat layout (a directory of *.json, no benchmark subdir)
python -m MAS_trajectory_analysis.run --input-dir /path/to/bench --output-dir /path/to/out

# smoke test: segmentation only, NO LLM calls, NO API key needed
python -m MAS_trajectory_analysis.run --benchmark swe_bench_pro --dry-run --limit 3

# one file + full intermediate results in <name>.debug.json, DEBUG logs
python -m MAS_trajectory_analysis.run --benchmark swe_bench_pro --file some.json --debug-sidecar -v

# verify an output is byte-identical to its source except the 4 injected fields
python -m MAS_trajectory_analysis.tools.verify_diff --benchmark swe_bench_pro
```

There is **no test suite and no linter config**. `--dry-run` is the fast sanity check (exercises preprocessing/segmentation without spending API tokens); `tools/verify_diff.py` is the output-integrity check. Default input/output paths resolve to the repo's `Who&When_style/<bench>/` and `Who&When_style/MAS_trajectory_analysis/<bench>/` (see `config.py`).

## Pipeline architecture

`run.py::process_one` orchestrates one file through five stages. The data objects passed between them are dataclasses in `core/schema.py` (`Step → Segment → LocalSummary → PhaseSummary → RootCauseAnnotation`).

1. **Preprocess** (`preprocess/`, no LLM): `loader.load_trajectory` reads the raw JSON and `step_enricher.enrich_steps` heuristically derives each `Step` — normalized agent role, `action_type`, `verifier_signal`, `exit_code`, and a `step_hash` fingerprint. `segmenter.segment_trajectory` then cuts the steps into ~24k-char segments using hybrid boundaries (hard: finish/verifier-flip/agent-shift/length/step caps; soft: target-length + agent-shift) with a 5-step overlap tail for context.
2. **Local summary** (`analyze/local_summarizer.py`): one LLM call **per segment** → `LocalSummary` (segment goal, key events, candidate failures).
3. **Phase aggregation** (`analyze/global_reducer.py::aggregate_phases`): one LLM call over all local summaries → `PhaseSummary` of 3–8 phases + cross-phase conflicts.
4. **Root cause** (`analyze/global_reducer.py::diagnose_root_cause`): one LLM call → `RootCauseAnnotation`. Before the call it gathers candidate failure steps **plus the nearest preceding `delegate` step** for each (`_augment_with_preceding_delegates`) so blame can land on the planner's flawed delegation rather than the executor's faithful implementation; delegate steps also get a longer content excerpt.
5. **Present** (`output/presenter.py::build_lean_summary`, no LLM): derives the lean, display-only `llm_analysis_summary` from the full internal summary. `io_writer.inject_v2_fields` + `write_v2_result` then copies the original and writes the result (atomic write + per-path file lock for `--workers` safety).

## Invariants to preserve when changing anything

- **Validate → retry-once → coerce.** Every LLM stage in `run.py` follows the same shape: call the stage, `validate_*` (in `analyze/validator.py`); if invalid, re-call **with `previous_errors` fed back into the prompt**; if still invalid, `coerce_*` (drop out-of-range ids/enums) and set `needs_human_review` (root cause additionally falls back to `abstain`). Keep this pattern when adding a stage. `aggregate_phases`/`diagnose_root_cause`/`summarize_segment` also have inline fallbacks when JSON parsing fails entirely.
- **Anti-drift: every `step_id` must be real.** All `evidence_step_ids`, `failure_chain` steps, `supporting_step_ids`, etc. are validated against the global step set and filtered again in the presenter (`_clean_ids`). The responsible `agent` must be in the trajectory's raw agent names ∪ `config.SPECIAL_AGENTS`. `sub_phases` are deterministically re-tiled to cover each phase with no gaps (`presenter._tile_subphases`) — the LLM's sub-phase ranges are treated as hints, not truth.
- **Output is byte-identical + exactly 4 injected top-level fields:** `llm_mistake_agent`, `llm_mistake_step` (int; `-1` = the `system_evaluation` pseudo-step), `llm_mistake_reason`, and `llm_analysis_summary`. The first three mirror the dataset's Who&When `mistake_*` slots; categories live **only** inside `llm_analysis_summary.root_cause`, never at top level. `tools/verify_diff.py` enforces this contract.
- **`config.py` is the single source of truth** for paths, API config, all hyperparameters (segmentation sizes, token limits, temperatures), and the **taxonomy** (`CATEGORY_META`, `CATEGORY_MAIN_LABELS`, `CATEGORY_MAIN_PRIORITY`). `validator.py` and `presenter.py` derive their category enums from `CATEGORY_META` — never hard-code category codes elsewhere.
- **Prompts are the system prompts.** `prompts/local_summary.md`, `phase_aggregate.md`, `root_cause.md` are loaded lazily and sent verbatim as the system message. Changing the taxonomy or output schema means editing **both** `config.py` and the corresponding prompt file (and bumping `PROMPT_VERSIONS` / `SCHEMA_VERSION`).
- `is_correct: true` trajectories are skipped (only failures are analyzed). Already-complete outputs are skipped on resume unless `--overwrite` (`io_writer.is_complete_v2`).

## Adding support for a new agent framework

Agent-role recognition is heuristic. Unknown agent names fall to `unknown` (not fatal — only weakens segmentation). To recognize a new framework's planner/executor/verifier names precisely, add them to the `PLANNERS` / `EXECUTORS` / `VERIFIERS` sets in `preprocess/step_enricher.py`. A delegation step is encoded as the agent name `X (-> Y)` and is treated as an independent, blameable actor.
