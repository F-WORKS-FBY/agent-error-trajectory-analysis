"""Stage 3 (aggregate_phases) + Stage 4 (diagnose_root_cause)。"""
from __future__ import annotations

import json
import logging
import re
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
        reasoning_effort=config.LLM_REASONING_EFFORT_PHASE,
        thinking=config.LLM_THINKING_PHASE,
        stage="phase",
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
    # Round 6:候选 step 一律回拉**完整正文**(决定性缺陷常埋在中段:委派计划的顺序/数据源/接口逻辑)。
    content = step.content_full
    if len(content) > config.STEP_FULL_MAX_CHARS:
        content = content[: config.STEP_FULL_MAX_CHARS] + "\n…[truncated, oversized step]"
    d: Dict[str, Any] = {
        "step_id": step.step_id,
        "agent": step.agent_name_raw,
        "action_type": step.action_type,
        "exit_code": step.exit_code,
        "verifier_signal": step.verifier_signal,
        "step_hash": step.step_hash,
        "content": content,
    }
    if step.action_type == "delegate":
        # 标注「规范来源」:缺陷常在 planner 的委派/计划里被引入,执行者只是实现它。
        d["role_hint"] = "SPEC(规范来源:planner 的委派/计划,缺陷可能在此引入,优先核查)"
    return d


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
    """为每个候选失败步,补上它之前最近的一次 **目标匹配的** delegate 步(planner 的委派/计划)。

    这样根因阶段才能判断缺陷是不是 planner 在委派里就埋下的(执行者只是忠实实现);
    否则只能看到执行者的实现步,容易把根因误判到"显露处"。

    目标匹配:executor/verifier 候选只补"委派给它这一角色"的委派(执行步配 `(-> 执行者)`、
    验证步配 `(-> 验证者)`),避免把一个执行步的 SPEC 上下文错配成 `(-> JudgeAgent)` 委派
    而抬高其显著性;其它候选(terminal/observation 等)退回"最近的任意委派"以保留计划上下文。
    """
    delegate_steps = sorted(
        (s for s in all_steps if s.action_type == "delegate"), key=lambda s: s.step_id
    )
    if not delegate_steps:
        return candidate_ids
    step_by_id = {s.step_id: s for s in all_steps}
    out: Set[int] = set(candidate_ids)
    for cid in candidate_ids:
        s = step_by_id.get(cid)
        if s is None:
            continue
        target = s.agent_name_raw if s.agent_normalized in ("executor", "verifier") else None
        deleg = _nearest_preceding_delegate(cid, target, delegate_steps)
        # executor/verifier 候选若找不到目标匹配的委派,**不**强行拉一个跨目标委派进来(避免错配 SPEC 显著性)。
        if deleg is None:
            continue
        out.add(deleg.step_id)
    return sorted(out)


# ----------------------------------------------------------------------------
# 执行步 ↔ 委派步「忠实实现」线索(确定性,零额外 LLM 调用)
# ----------------------------------------------------------------------------
# 执行者明确声称"照计划做"的措辞。命中即强信号:该执行步很可能只是忠实实现上游委派。
_RE_PLAN_CITE = re.compile(
    r"per the plan|as delegated|as instructed|as planned|as specified|"
    r"as (?:the )?plan (?:says|states|requires|specifies|dictates)|"
    r"following the (?:plan|delegation|instructions?|spec)|according to the plan|"
    r"按照?计划|照计划|按委派|遵循计划|依照计划|按计划要求",
    re.I,
)
# 像"代码标识符"的 token(camelCase / snake_case / 含数字),用于度量执行步与委派步的实现重叠度。
# 只取这类有区分度的 token,避免把普通英文词(should/return/...)算进去造成误命中。
_RE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{4,}")
_LINKAGE_OVERLAP_THRESHOLD = 0.34   # 共享标识符 / 执行步标识符 ≥ 此值才视为强重叠(overlap 分支)
# overlap 分支的两道防退化闸:① 共享标识符绝对数下限(短步只共享 1-2 个 token 时比值=1.0 是噪声);
# ② 执行步本身的 distinctive 标识符下限(太少则比值统计上无意义)。审计显示这能滤掉 ~57-65% 的退化命中。
_LINKAGE_MIN_SHARED = 3
_LINKAGE_MIN_EXEC_TOKENS = 5


def _delegate_target(agent_name_raw: str) -> Optional[str]:
    """委派态名的目标角色:'DiagnostAgent (-> ActionAgent)' -> 'ActionAgent';非委派名返回 None。"""
    n = agent_name_raw or ""
    if " (-> " in n and n.endswith(")"):
        return n.split(" (-> ", 1)[1][:-1].strip()
    return None


def _nearest_preceding_delegate(
    cid: int, target_name: Optional[str], delegate_steps: List[Step]
) -> Optional[Step]:
    """cid 之前、**目标 == target_name** 的最近一次 delegate(`delegate_steps` 须按 step_id 升序)。

    target_name=None 时退回"最近的任意 delegate"。目标过滤是为了防止把一个执行步错链到
    `(-> JudgeAgent)` 的委派上(那条委派规范的是验证者的任务,不是这个执行步要实现的东西)。
    """
    best: Optional[Step] = None
    for d in delegate_steps:
        if d.step_id >= cid:
            break
        if target_name is None or _delegate_target(d.agent_name_raw) == target_name:
            best = d
    return best


def _distinctive_tokens(text: str) -> Set[str]:
    toks: Set[str] = set()
    for m in _RE_IDENTIFIER.finditer(text or ""):
        t = m.group(0)
        if len(t) < 6:
            continue
        # 仅保留明显是代码标识符的(含下划线 / 内部大写 / 数字),滤掉普通英文词。
        if "_" in t or any(c.isupper() for c in t[1:]) or any(c.isdigit() for c in t):
            toks.add(t)
    return toks


def _build_delegate_linkage_hints(
    candidate_ids: List[int], all_steps: List[Step]
) -> List[Dict[str, Any]]:
    """为每个**执行者**候选步,判断它是否在「忠实实现」**委派给它**的最近一次委派。

    命中条件(任一):
    - **strong** —— 正文显式声称"照计划/按委派"(`_RE_PLAN_CITE`),或
    - **weak**   —— 与该委派步共享足够多的代码标识符(`overlap≥阈值` 且 `shared≥3` 且 `exec_tokens≥5`)。

    三道精度闸(防过度归因 planner):
    1. **目标匹配**:执行步只链接到目标==该执行者的委派(`(-> ActionAgent)` 等),**绝不**链到 `(-> JudgeAgent)`。
    2. **overlap 绝对下限**:共享标识符 <3 或执行步标识符 <5 时不走 weak 分支(滤掉短步 overlap=1.0 噪声)。
    3. hint 自带 `strength`,弱线索仅代表"作用域重叠",不足以单独支撑回溯。

    命中只是**确定性启发线索**(不强制结论),模型仍须在 `delegation_trace` 里引用被照搬的委派原话来核实。
    """
    step_by_id = {s.step_id: s for s in all_steps}
    delegate_steps = sorted(
        (s for s in all_steps if s.action_type == "delegate"), key=lambda s: s.step_id
    )
    if not delegate_steps:
        return []
    hints: List[Dict[str, Any]] = []
    for cid in candidate_ids:
        s = step_by_id.get(cid)
        if not s or s.agent_normalized != "executor":
            continue
        # 目标匹配:只找"委派给这个执行者"的最近前驱委派;找不到就不发 hint(不跨目标错链)。
        deleg = _nearest_preceding_delegate(cid, s.agent_name_raw, delegate_steps)
        if deleg is None:
            continue
        m = _RE_PLAN_CITE.search(s.content_full or "")
        exec_tok = _distinctive_tokens(s.content_full)
        shared = exec_tok & _distinctive_tokens(deleg.content_full)
        overlap = (len(shared) / len(exec_tok)) if exec_tok else 0.0
        overlap_fire = (
            len(exec_tok) >= _LINKAGE_MIN_EXEC_TOKENS
            and len(shared) >= _LINKAGE_MIN_SHARED
            and overlap >= _LINKAGE_OVERLAP_THRESHOLD
        )
        if not m and not overlap_fire:
            continue
        strength = "strong" if m else "weak"
        why = (
            f"显式声称「{m.group(0)}」" if m
            else f"与委派共享 {len(shared)} 个关键标识符(重叠 {overlap:.0%})"
        )
        hints.append({
            "executor_step": cid,
            "executor_agent": s.agent_name_raw,
            "prescribed_by_delegate_step": deleg.step_id,
            "delegate_agent": deleg.agent_name_raw,
            "delegate_target": _delegate_target(deleg.agent_name_raw),
            "strength": strength,
            "cites_plan": (m.group(0) if m else None),
            "overlap": round(overlap, 2),
            "shared_count": len(shared),
            "shared_identifiers": sorted(shared)[:10],
            "note": (
                f"执行步 {cid} 疑为**忠实实现** delegate 步 {deleg.step_id}({why};强度={strength})。"
                f"按 §2.3:**仅当**执行步一字不差照搬了该委派里写明的同一个缺陷,才回溯到 delegate 步 {deleg.step_id}"
                f"(agent 用委派态名 {deleg.agent_name_raw},primary=C1_flawed_plan),执行步 {cid} 标 propagation。"
                f"strength=weak 仅表示作用域/标识符重叠(改同一文件/函数),**不等于**缺陷来自委派——"
                f"若执行者偏离/误读/新增了计划之外的错误,根因**留在执行步**。"
                f"务必在 delegation_trace 中引用被照搬的委派原话并据实置 defect_in_quote。"
            ),
        })
    return hints


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
    # 确定性「忠实实现」线索:执行步是否只是在照搬某个上游委派 → 提示根因回溯到 delegate。
    delegate_linkage_hints = _build_delegate_linkage_hints(candidate_ids, all_steps)

    payload = {
        "task_brief": task_brief,
        "is_correct": raw_data.get("is_correct", False),
        "valid_step_ids": compress_step_ids(sorted(global_step_ids)),
        "valid_agents": valid_agents_raw,
        "phases": phases.to_dict(),
        "local_summaries": [ls.to_dict() for ls in locals_],
        # 放在 excerpts 之前,优先被模型读到。
        "delegate_linkage_hints": delegate_linkage_hints,
        "candidate_steps_excerpts": excerpts,
    }
    hint_line = (
        "**特别注意** `delegate_linkage_hints`(执行步↔委派步的忠实实现线索):"
        "逐条核对,据此判断根因该不该回溯到 delegate 步(见 §2.3),并填写 `delegation_trace`。\n"
        if delegate_linkage_hints else ""
    )
    user = (
        "## 任务\n"
        "依据下方 phases + local_summaries + candidate_steps_excerpts,"
        "判定**最早的、不可恢复的、真正导致级联失败的根因**,并严格按 prompt §8 输出 JSON。\n"
        + hint_line +
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
        reasoning_effort=config.LLM_REASONING_EFFORT_ROOT,
        thinking=config.LLM_THINKING_ROOT,
        stage="root",
    )
    reasoning_trace = client.last_reasoning_content     # 思维链(仅供 debug sidecar 审计)
    ann = RootCauseAnnotation(raw_text=raw)
    if parsed is None:
        LOG.warning("root cause JSON parse failed (finish=%s, raw_len=%d)", finish, len(raw or ""))
        ann.abstain = True
        ann.needs_human_review = True
        ann.reason = f"[LLM 输出解析失败] raw_len={len(raw or '')} finish={finish}"
        ann.detailed_analysis = {"raw_output": (raw or "")[:5000], "confidence": "low"}
        if reasoning_trace:
            ann.detailed_analysis["_root_reasoning_trace"] = reasoning_trace
        return ann

    ann.agent = str(parsed.get("agent") or "").strip()
    ann.step = str(parsed.get("step") or "").strip()
    ann.reason = str(parsed.get("reason") or "").strip()
    ann.evidence_step_ids = list(parsed.get("evidence_step_ids") or [])
    ann.abstain = bool(parsed.get("abstain", False))
    ann.primary_category = str(parsed.get("primary_category") or "").strip()
    ann.contributing_factors = list(parsed.get("contributing_factors") or [])
    ann.detailed_analysis = parsed.get("detailed_analysis") or {}
    # 思维链仅落 debug sidecar(presenter 不读此键,主输出不受影响)。
    if reasoning_trace and isinstance(ann.detailed_analysis, dict):
        ann.detailed_analysis["_root_reasoning_trace"] = reasoning_trace
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
