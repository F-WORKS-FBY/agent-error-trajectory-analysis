"""Stage 3 (aggregate_phases) + Stage 4 (diagnose_root_cause)。"""
from __future__ import annotations

import bisect
import json
import logging
from typing import Dict, Any, List, Optional, Set

from .. import config
from ..core.schema import Step, Segment, LocalSummary, PhaseSummary, RootCauseAnnotation
from ..core.llm_client import DeepSeekClient

LOG = logging.getLogger("MAS_trajectory_analysis.global")

_PHASE_PROMPT: Optional[str] = None
_ROOT_PROMPT: Optional[str] = None


def _load_phase_prompt() -> str:
    global _PHASE_PROMPT
    if _PHASE_PROMPT is None:
        _PHASE_PROMPT = (config.PROMPTS_DIR / "phase_aggregate.md").read_text(encoding="utf-8")
    return _PHASE_PROMPT


def _load_root_prompt() -> str:
    global _ROOT_PROMPT
    if _ROOT_PROMPT is None:
        _ROOT_PROMPT = (config.PROMPTS_DIR / "root_cause.md").read_text(encoding="utf-8")
    return _ROOT_PROMPT


# ----------------------------------------------------------------------------
# Step IDs compaction
# ----------------------------------------------------------------------------
def compress_step_ids(step_ids: List[int]) -> str:
    if not step_ids:
        return ""
    ids = sorted(set(step_ids))
    out = []
    start = prev = ids[0]
    for x in ids[1:]:
        if x == prev + 1:
            prev = x
            continue
        out.append(f"{start}-{prev}" if start != prev else f"{start}")
        start = prev = x
    out.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ",".join(out)


# ----------------------------------------------------------------------------
# Phase aggregation
# ----------------------------------------------------------------------------
def aggregate_phases(
    client: DeepSeekClient,
    locals_: List[LocalSummary],
    task_brief: str,
    global_step_ids: Set[int],
    verifier_signal_summary: Dict[str, Any],
    previous_errors: Optional[List[str]] = None,
) -> PhaseSummary:
    system = _load_phase_prompt()
    payload = {
        "task_brief": task_brief,
        "all_step_ids_compressed": compress_step_ids(sorted(global_step_ids)),
        "verifier_signal_summary": verifier_signal_summary,
        "local_summaries": [ls.to_dict() for ls in locals_],
    }
    user = (
        "## 任务\n"
        "请聚合下方 local_summaries 为 3-8 个 phase,严格按 prompt §输出要求输出 JSON。\n\n"
        "## 数据(JSON)\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    if previous_errors:
        user += (
            "\n\n## 上一次输出的错误,请修正\n"
            + "\n".join(f"- {e}" for e in previous_errors[:20])
        )

    parsed, raw, finish = client.chat_json(
        system=system, user=user,
        temperature=config.LLM_TEMPERATURE_DEFAULT,
        max_tokens=config.LLM_MAX_TOKENS_PHASE,
    )
    ps = PhaseSummary(raw_text=raw)
    if parsed is None:
        LOG.warning("phase aggregation JSON parse failed (finish=%s, raw_len=%d)", finish, len(raw or ""))
        # 兜底:每个 local summary 视为一个 phase
        fallback_phases = []
        for ls in locals_:
            fallback_phases.append({
                "phase_id": ls.segment_id,
                "step_range": ls.step_range,
                "phase_goal": ls.segment_goal or "(fallback) segment",
                "involved_agents": [],
                "sub_phases": [
                    {"step_range": list(ls.step_range),
                     "description": ls.segment_goal or "(fallback) segment"}
                ],
                "failure_signals": [],
                "supporting_step_ids": [],
            })
        ps.phases = fallback_phases
        return ps

    ps.phases = list(parsed.get("phases") or [])
    ps.conflicts = list(parsed.get("conflicts") or [])
    return ps


# ----------------------------------------------------------------------------
# Root cause
# ----------------------------------------------------------------------------
def _excerpt_step(step: Step) -> Dict[str, Any]:
    # delegate/plan 步是"规范来源",决定性缺陷常埋在中段;给它更长的正文,避免 500 字头尾截断丢掉关键顺序/逻辑。
    if step.action_type == "delegate":
        head = step.content_full[:2400]
    else:
        head = step.content_head
    return {
        "step_id": step.step_id,
        "agent": step.agent_name_raw,
        "action_type": step.action_type,
        "exit_code": step.exit_code,
        "verifier_signal": step.verifier_signal,
        "step_hash": step.step_hash,
        "content_head": head,
        "content_tail": step.content_tail or None,
    }


def _collect_candidate_step_ids(locals_: List[LocalSummary], phases: PhaseSummary) -> List[int]:
    ids: Set[int] = set()
    for ls in locals_:
        for cf in (ls.candidate_failures or []):
            for sid in (cf.get("step_ids") or []):
                try:
                    ids.add(int(sid))
                except (TypeError, ValueError):
                    continue
    for ph in (phases.phases or []):
        for fs in (ph.get("failure_signals") or []):
            for sid in (fs.get("step_ids") or []):
                try:
                    ids.add(int(sid))
                except (TypeError, ValueError):
                    continue
        # supporting_step_ids 中前 3 个也回拉
        for sid in (ph.get("supporting_step_ids") or [])[:3]:
            try:
                ids.add(int(sid))
            except (TypeError, ValueError):
                continue
    return sorted(ids)


def _augment_with_preceding_delegates(candidate_ids: List[int], all_steps: List[Step]) -> List[int]:
    """为每个候选失败步,补上它之前最近的一次 delegate 步(planner 的委派/计划)。

    这样根因阶段才能判断缺陷是不是 planner 在委派里就埋下的(执行者只是忠实实现);
    否则只能看到执行者的实现步,容易把根因误判到"显露处"。
    """
    delegate_ids = sorted(s.step_id for s in all_steps if s.action_type == "delegate")
    if not delegate_ids:
        return candidate_ids
    out: Set[int] = set(candidate_ids)
    for cid in candidate_ids:
        pos = bisect.bisect_right(delegate_ids, cid) - 1
        if pos >= 0:
            out.add(delegate_ids[pos])
    return sorted(out)


def diagnose_root_cause(
    client: DeepSeekClient,
    phases: PhaseSummary,
    locals_: List[LocalSummary],
    all_steps: List[Step],
    raw_data: Dict[str, Any],
    task_brief: str,
    global_step_ids: Set[int],
    valid_agents_raw: List[str],
    previous_errors: Optional[List[str]] = None,
) -> RootCauseAnnotation:
    system = _load_root_prompt()

    step_by_id = {s.step_id: s for s in all_steps}
    candidate_ids = _collect_candidate_step_ids(locals_, phases)
    # 对每个候选失败步,把它**之前最近的一次 delegate**也拉进来,
    # 让根因阶段能看到"是不是 planner 的委派/计划本身有缺陷",而非只盯着执行者的实现步。
    candidate_ids = _augment_with_preceding_delegates(candidate_ids, all_steps)
    excerpts = [
        _excerpt_step(step_by_id[i]) for i in candidate_ids if i in step_by_id
    ]

    payload = {
        "task_brief": task_brief,
        "is_correct": raw_data.get("is_correct", False),
        "valid_step_ids": compress_step_ids(sorted(global_step_ids)),
        "valid_agents": valid_agents_raw,
        "phases": phases.to_dict(),
        "local_summaries": [ls.to_dict() for ls in locals_],
        "candidate_steps_excerpts": excerpts,
    }
    user = (
        "## 任务\n"
        "依据下方 phases + local_summaries + candidate_steps_excerpts,"
        "判定**最早的、不可恢复的、真正导致级联失败的根因**,并严格按 prompt §8 输出 JSON。\n"
        "所有 step / agent / evidence_step_ids 必须严格符合 §9 约束。中文输出。\n\n"
        "## 数据(JSON)\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    if previous_errors:
        user += (
            "\n\n## 上一次输出的错误,请修正\n"
            + "\n".join(f"- {e}" for e in previous_errors[:30])
        )

    parsed, raw, finish = client.chat_json(
        system=system, user=user,
        temperature=config.LLM_TEMPERATURE_ROOT,
        max_tokens=config.LLM_MAX_TOKENS_ROOT,
    )
    ann = RootCauseAnnotation(raw_text=raw)
    if parsed is None:
        LOG.warning("root cause JSON parse failed (finish=%s, raw_len=%d)", finish, len(raw or ""))
        ann.abstain = True
        ann.needs_human_review = True
        ann.reason = f"[LLM 输出解析失败] raw_len={len(raw or '')} finish={finish}"
        ann.detailed_analysis = {"raw_output": (raw or "")[:5000], "confidence": "low"}
        return ann

    ann.agent = str(parsed.get("agent") or "").strip()
    ann.step = str(parsed.get("step") or "").strip()
    ann.reason = str(parsed.get("reason") or "").strip()
    ann.evidence_step_ids = list(parsed.get("evidence_step_ids") or [])
    ann.abstain = bool(parsed.get("abstain", False))
    ann.primary_category = str(parsed.get("primary_category") or "").strip()
    ann.contributing_factors = list(parsed.get("contributing_factors") or [])
    ann.detailed_analysis = parsed.get("detailed_analysis") or {}
    return ann


# ----------------------------------------------------------------------------
# Verifier signal summary helper
# ----------------------------------------------------------------------------
def build_verifier_signal_summary(steps: List[Step]) -> Dict[str, Any]:
    sigs = [(s.step_id, s.verifier_signal) for s in steps if s.verifier_signal]
    return {
        "total_signals": len(sigs),
        "first_pass_step": next((sid for sid, sig in sigs if sig == "PASS"), None),
        "first_fail_step": next((sid for sid, sig in sigs if sig == "FAIL"), None),
        "last_signal": sigs[-1] if sigs else None,
        "sequence_preview": sigs[:20],
    }
