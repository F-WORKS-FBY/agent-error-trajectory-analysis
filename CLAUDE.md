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
#   NOTE base_url ends at /v1 (e.g. https://api.siliconflow.cn/v1) — do NOT append /chat/completions.
# per-stage override (mix models/providers): LLM_{MODEL,BASE_URL,API_KEY}_{LOCAL,PHASE,ROOT}, each
#   falling back to the global LLM_* (so single-model setups are unchanged). See `config.STAGE_LLM`.
# NOTE: no python-dotenv dep — .env is NOT auto-loaded. `cp .env.example .env` alone does nothing;
# you must `export` the vars or `set -a; source .env` (or override per-run with --model / --base-url).

# classic layout: <input-dir>/<benchmark>/*.json
cd ../  &&  python -m MAS_trajectory_analysis.run --benchmark swe_bench_pro --workers 4

# flat layout (a directory of *.json, no benchmark subdir)
python -m MAS_trajectory_analysis.run --input-dir /path/to/bench --output-dir /path/to/out

# arbitrary MAS / arbitrary format: pick a dataset profile (or omit --profile to auto-sniff)
python -m MAS_trajectory_analysis.run --input-dir /path/to/Who\&When/Hand-Crafted \
    --output-dir /tmp/ww --profile who_and_when      # built-in: default | who_and_when | <path>.json

# smoke test: segmentation only, NO LLM calls, NO API key needed
python -m MAS_trajectory_analysis.run --benchmark swe_bench_pro --dry-run --limit 3

# one file + full intermediate results in <name>.debug.json, DEBUG logs
python -m MAS_trajectory_analysis.run --benchmark swe_bench_pro --file some.json --debug-sidecar -v

# verify an output is byte-identical to its source except the 4 injected fields
python -m MAS_trajectory_analysis.tools.verify_diff --benchmark swe_bench_pro

# Who&When has ground-truth who/when/why → score the method's accuracy against it
python -m MAS_trajectory_analysis.tools.eval_who_and_when --dir /tmp/ww --show-misses
```

### Batch runs & monitoring

The shell scripts are the normal way to run a real (non-smoke) job. They `source` the sibling `.env` themselves, resolve paths regardless of CWD, support resume (re-run = skip-completed), and **default to a different data root than `config.py`** — `CNIC/zhangyunfei/{data,llm_analysis}` instead of `Who&When_style/` (override with `DATA_DIR=` / `OUT_DIR=`).

```bash
./run_swe.sh                              # just swe_bench_pro (pre-flight quality check before a big run)
WORKERS=10 ./run_all_benches.sh           # all 5 benches; resume-safe (re-run to continue after a crash)
OVERWRITE=1 WORKERS=10 ./run_all_benches.sh   # ONLY for a prompt change — full recompute (drop OVERWRITE on resume!)
LIMIT=3 ./run_all_benches.sh swe_bench_pro     # env knobs: WORKERS / OVERWRITE / DEBUG_SIDECAR / LIMIT / DATA_DIR / OUT_DIR

# global progress across all benches (run in a SECOND terminal during a long job)
python -m MAS_trajectory_analysis.tools.progress --watch 30   # done/total per bench by counting output files

# per-stage thinking benchmark: runs 4 anchor trajectories with known-correct root causes,
# asserts no regression, and reports latency + token cost per stage (writes to a scratch dir)
python -m MAS_trajectory_analysis.tools.bench_thinking
```

There is **no test suite and no linter config**. `--dry-run` is the fast sanity check (exercises preprocessing/segmentation without spending API tokens); `tools/verify_diff.py` is the output-integrity check; `tools/bench_thinking.py` is the closest thing to a regression test (4 hard-coded anchor cases). Default `--input-dir`/`--output-dir` resolve to the repo's `Who&When_style/<bench>/` and `Who&When_style/MAS_trajectory_analysis/<bench>/` (see `config.py`), but the batch scripts point elsewhere (above).

## Pipeline architecture

`run.py::process_one` orchestrates one file through five stages. The data objects passed between them are dataclasses in `core/schema.py` (`Step → Segment → LocalSummary → PhaseSummary → RootCauseAnnotation`).

1. **Preprocess** (`preprocess/`, no LLM): `loader.load_trajectory(src, profile=None)` reads the raw JSON, resolves a **`DatasetProfile`** (`preprocess/profile.py`; sniffed if not given), and returns `(data, steps, profile)`. `step_enricher.enrich_steps(history, profile)` heuristically derives each `Step` — agent name + normalized role, `action_type`, `verifier_signal`, `exit_code`, `delegate_target`, and a `step_hash` fingerprint — all field/role/delegation mappings come from the profile. `segmenter.segment_trajectory` then cuts the steps into ~24k-char segments using hybrid boundaries (hard: finish/verifier-flip/agent-shift/length/step caps; soft: target-length + agent-shift) with a 5-step overlap tail for context.
2. **Local summary** (`analyze/local_summarizer.py`): one LLM call **per segment** → `LocalSummary` (segment goal, key events, candidate failures).
3. **Phase aggregation** (`analyze/global_reducer.py::aggregate_phases`): one LLM call over all local summaries → `PhaseSummary` of 3–8 phases + cross-phase conflicts.
4. **Root cause** (`analyze/global_reducer.py::diagnose_root_cause`): one LLM call → `RootCauseAnnotation`. Before the call it gathers candidate failure steps **plus the nearest preceding `delegate` step** for each (`_augment_with_preceding_delegates`) so blame can land on the planner's flawed delegation rather than the executor's faithful implementation; delegate steps also get a longer content excerpt.
5. **Present** (`output/presenter.py::build_lean_summary`, no LLM): derives the lean, display-only `llm_analysis_summary` from the full internal summary. `io_writer.inject_v2_fields` + `write_v2_result` then copies the original and writes the result (atomic write + per-path file lock for `--workers` safety).

## Invariants to preserve when changing anything

- **Validate → retry-once → coerce.** Every LLM stage in `run.py` follows the same shape: call the stage, `validate_*` (in `analyze/validator.py`); if invalid, re-call **with `previous_errors` fed back into the prompt**; if still invalid, `coerce_*` (drop out-of-range ids/enums) and set `needs_human_review` (root cause additionally falls back to `abstain`). Keep this pattern when adding a stage. `aggregate_phases`/`diagnose_root_cause`/`summarize_segment` also have inline fallbacks when JSON parsing fails entirely.
- **Anti-drift: every `step_id` must be real.** All `evidence_step_ids`, `failure_chain` steps, `supporting_step_ids`, etc. are validated against the global step set and filtered again in the presenter (`_clean_ids`). The responsible `agent` must be in the trajectory's raw agent names ∪ `config.SPECIAL_AGENTS`. `sub_phases` are deterministically re-tiled to cover each phase with no gaps (`presenter._tile_subphases`) — the LLM's sub-phase ranges are treated as hints, not truth.
- **Output is byte-identical + exactly 4 injected top-level fields:** `llm_mistake_agent`, `llm_mistake_step` (int; `-1` = the `system_evaluation` pseudo-step), `llm_mistake_reason`, and `llm_analysis_summary`. The first three mirror the dataset's Who&When `mistake_*` slots; categories live **only** inside `llm_analysis_summary.root_cause`, never at top level. `tools/verify_diff.py` enforces this contract.
- **`config.py` is the single source of truth** for paths, API config, all hyperparameters (segmentation sizes, token limits, temperatures), and the **taxonomy** (`CATEGORY_META`, `CATEGORY_MAIN_LABELS`, `CATEGORY_MAIN_PRIORITY`). `validator.py` and `presenter.py` derive their category enums from `CATEGORY_META` — never hard-code category codes elsewhere.
- **Thinking is configured per stage, not globally** (sent via `extra_body` in `core/llm_client.py`; `LLM_THINKING_ENABLED` is only the fallback for calls that don't pass an explicit `thinking=`). As of Round 7: Stage 2 local summary has thinking **off** (`LLM_THINKING_LOCAL=False` — it's an extraction task and ~84% of all calls, so this is the main speed/cost lever), while Stages 3/4 (phase, root cause) run thinking on. **Temperature is inert only while thinking is on** — so `LLM_TEMPERATURE_DEFAULT=0.1` is actually live for Stage 2 now, but the root-cause temperature stays inert. When tuning a stage, change *that stage's* `LLM_THINKING_*`/`LLM_REASONING_EFFORT_*` pair. The client strips `<think>` blocks from responses and exposes the chain-of-thought via `last_reasoning_content`; per-call token/latency is recorded to `last_usage` and (if a `metrics_sink` list is attached) appended there — that's how `bench_thinking.py` measures cost.
- **The `thinking`/`reasoning_effort` `extra_body` is DeepSeek-API-private.** `core/llm_client.py` only sends it when `DeepSeekClient.thinking_style == "deepseek"` (inferred from base_url, override via `LLM_THINKING_STYLE`). For any other endpoint (e.g. SiliconFlow) it sends **nothing** (`"none"`) so a non-reasoning model won't 400. If you point a stage at a reasoning endpoint, set the style accordingly.
- **Per-stage clients.** `run.py::main` builds `build_stage_clients()` → `{local, phase, root}` `DeepSeekClient`s from `config.STAGE_LLM`, deduped so identical `(model, base_url, api_key)` triples share one instance. `process_one(..., clients, ..., profile=...)` routes each stage to its client. `--model`/`--base-url` are **global** overrides across all stages (back-compat single-client behavior).
- **Prompts are the system prompts.** `prompts/local_summary.md`, `phase_aggregate.md`, `root_cause.md` are loaded lazily and sent verbatim as the system message. Changing the taxonomy or output schema means editing **both** `config.py` and the corresponding prompt file (and bumping `PROMPT_VERSIONS` / `SCHEMA_VERSION`).
- `is_correct: true` trajectories are skipped (only failures are analyzed). Already-complete outputs are skipped on resume unless `--overwrite` (`io_writer.is_complete_v2`).

## Adding support for a new MAS framework / dataset format (generalized)

Input parsing is driven by a **`DatasetProfile`** (`preprocess/profile.py`), not hard-coded. To onboard a new framework you usually write/sniff one profile — **no core code changes**:

- **Field mapping**: `history_key`, `step_id_field` (`None` → enumerate by index — required for datasets with no `step` field), `role_field`, `agent_name_field`, `agent_from_role` (agent name lives in the role field), `content_field`, `is_correct_field`, and the task-text fields (`question_field`/`ground_truth_field`/`verifier_field`/...).
- **Role mapping**: `role_mode="mapped"` uses the `planners/executors/verifiers/terminals/humans` sets (default = OpenHands + Magentic-One, reproducing old behavior); `role_mode="passthrough"` keeps each raw agent name as its own role (still normalizes terminals/humans) — best for fully arbitrary MAS.
- **Delegation/handoff** (`DelegationSpec`): an ordered list of rules tried in order — `name_regex` (suffix like `X (-> Y)`, the default), `content_regex` (tool-call/orchestrator handoffs like `transfer_to_agent("Y")` / `next speaker: Y`), or `field` (a `to`/`recipient` key). The resolved target is stored once on `Step.delegate_target`; `action_type=="delegate"` iff it's non-`None`. **Nothing downstream parses agent-name strings** — `global_reducer`/`validator` read `Step.delegate_target`.

Use a built-in (`default`, `who_and_when`) via `--profile <name>`, point at a `--profile path/to/profile.json`, or omit `--profile` to **auto-sniff** (`sniff_profile` detects the history key, step-id presence, and whether the agent name is in `name` vs `role`). Profiles that don't recognize planner roles simply make the planner de-bias logic in `validator._validate_planner_attribution` a safe no-op.

When you do want precise role recognition for a `mapped` framework, extend the sets in `preprocess/profile.py` (`DEFAULT_PLANNERS` etc., re-exported from `step_enricher` for back-compat) or pass them in a custom profile.
