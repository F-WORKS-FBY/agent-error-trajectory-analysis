"""LocalSummary / PhaseSummary / RootCauseAnnotation 的结构校验 + 越界检查 + 软修正。

校验失败时返回 (ok=False, errors=[...]) 给上层决定是否重生成。
"""
from __future__ import annotations

import re
from typing import Dict, Any, List, Optional, Set, Tuple

from .. import config
from ..core.schema import LocalSummary, PhaseSummary, RootCauseAnnotation, Step


# planner 形态归因(裸 planner 或委派态)**不得**配"执行/验证/非agent"类主因——
# 那是执行者/验证者/环境的责任(防过度归因)。允许理解(A)/认知(B)/规划(C)类,因为 planner 确实做这些。
_PLANNER_PRIMARY_FORBIDDEN = frozenset({
    "D1_wrong_tool_or_args", "D2_unrecovered_tool_failure", "D3_stuck_or_repetition",
    "E1_verification_gap", "E2_premature_or_wrong_completion",
    "X1_underspecified_input", "X2_unrecoverable_environment",
})
# 忠实实现「有缺陷的计划」时,缺陷的两种主形态:计划逻辑本身错(C1),或计划无视了任务明示的方法/约束(A2)。
_FAITHFUL_PRIMARY_OK = frozenset({"C1_flawed_plan", "A2_ignored_constraint"})

_WS = re.compile(r"\s+")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _is_int_in(v: Any, allowed: Set[int]) -> bool:
    try:
        return int(v) in allowed
    except (TypeError, ValueError):
        return False


def _to_int(v: Any) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _norm_text(t: str) -> str:
    return _WS.sub(" ", (t or "")).strip().lower()


def _quote_grounded(quote: str, delegate_text: str) -> bool:
    """`quote` 是否确为 `delegate_text` 的(归一化)子串,或其词集 ≥80% 落在原文内。

    用于把模型"忠实实现"的声明变成可机器校验的事实:回溯到 planner 必须能在该委派步原文里
    找到被照搬的缺陷句。归一化折叠空白+小写,容忍排版差异;过短的 quote(<15 字符)不足以为证。
    """
    q, d = _norm_text(quote), _norm_text(delegate_text)
    if len(q) < 15:
        return False
    if q in d:
        return True
    qw = {w.lower() for w in _WORD.findall(quote or "")}
    if len(qw) >= 4:
        dw = {w.lower() for w in _WORD.findall(delegate_text or "")}
        return len(qw & dw) / len(qw) >= 0.8
    return False


def _step_field_ok(step_val: Any, allowed_int: Set[int]) -> bool:
    """`step` 字段 OK 条件:数字字符串 + int ∈ allowed,或 == 'system_evaluation'。"""
    if isinstance(step_val, int):
        return step_val in allowed_int
    s = str(step_val or "").strip()
    if not s:
        return False
    if s in config.ALLOWED_PSEUDO_STEPS:
        return True
    try:
        return int(s) in allowed_int
    except ValueError:
        return False


# ----------------------------------------------------------------------------
# LocalSummary
# ----------------------------------------------------------------------------
def validate_local_summary(ls: LocalSummary, segment_step_ids: Set[int]) -> Tuple[bool, List[str]]:
    errs: List[str] = []

    if not isinstance(ls.segment_id, int):
        errs.append(f"segment_id not int: {ls.segment_id!r}")

    if not (isinstance(ls.step_range, list) and len(ls.step_range) == 2):
        errs.append(f"step_range malformed: {ls.step_range!r}")

    for i, ev in enumerate(ls.key_events or []):
        if not isinstance(ev, dict):
            errs.append(f"key_events[{i}] not dict")
            continue
        for j, sid in enumerate(ev.get("step_ids") or []):
            if not _is_int_in(sid, segment_step_ids):
                errs.append(f"key_events[{i}].step_ids[{j}]={sid!r} out of segment")

    for i, cf in enumerate(ls.candidate_failures or []):
        if not isinstance(cf, dict):
            errs.append(f"candidate_failures[{i}] not dict")
            continue
        t = cf.get("type", "")
        if t not in config.LOCAL_FAILURE_TYPES:
            errs.append(f"candidate_failures[{i}].type={t!r} not in enum")
        for j, sid in enumerate(cf.get("step_ids") or []):
            if not _is_int_in(sid, segment_step_ids):
                errs.append(f"candidate_failures[{i}].step_ids[{j}]={sid!r} out of segment")

    for i, vf in enumerate(ls.verifier_findings or []):
        if not isinstance(vf, dict):
            errs.append(f"verifier_findings[{i}] not dict")
            continue
        r = vf.get("result", "")
        if r not in config.VERIFIER_RESULT_SET:
            errs.append(f"verifier_findings[{i}].result={r!r} not in enum")
        for j, sid in enumerate(vf.get("step_ids") or []):
            if not _is_int_in(sid, segment_step_ids):
                errs.append(f"verifier_findings[{i}].step_ids[{j}]={sid!r} out of segment")

    return (len(errs) == 0), errs


def coerce_local_summary(ls: LocalSummary, segment_step_ids: Set[int]) -> LocalSummary:
    """温和修正:剔除越界 step_ids、剔除越界 type/result。不改变其他字段。"""
    def _filter_ids(ids):
        out = []
        for x in (ids or []):
            try:
                xi = int(x)
                if xi in segment_step_ids:
                    out.append(xi)
            except (TypeError, ValueError):
                continue
        return out

    new_events = []
    for ev in (ls.key_events or []):
        if isinstance(ev, dict):
            ev = dict(ev)
            ev["step_ids"] = _filter_ids(ev.get("step_ids"))
            new_events.append(ev)
    ls.key_events = new_events

    new_cf = []
    for cf in (ls.candidate_failures or []):
        if not isinstance(cf, dict):
            continue
        cf = dict(cf)
        if cf.get("type") not in config.LOCAL_FAILURE_TYPES:
            cf["type"] = "none"
        cf["step_ids"] = _filter_ids(cf.get("step_ids"))
        new_cf.append(cf)
    ls.candidate_failures = new_cf

    new_vf = []
    for vf in (ls.verifier_findings or []):
        if not isinstance(vf, dict):
            continue
        vf = dict(vf)
        if vf.get("result") not in config.VERIFIER_RESULT_SET:
            vf["result"] = "UNKNOWN"
        vf["step_ids"] = _filter_ids(vf.get("step_ids"))
        new_vf.append(vf)
    ls.verifier_findings = new_vf

    return ls


# ----------------------------------------------------------------------------
# PhaseSummary
# ----------------------------------------------------------------------------
def validate_phase_summary(ps: PhaseSummary, global_step_ids: Set[int]) -> Tuple[bool, List[str]]:
    errs: List[str] = []
    if not isinstance(ps.phases, list) or not ps.phases:
        errs.append("phases empty or not list")
        return False, errs
    for i, ph in enumerate(ps.phases):
        if not isinstance(ph, dict):
            errs.append(f"phases[{i}] not dict")
            continue
        for j, sid in enumerate(ph.get("supporting_step_ids") or []):
            if not _is_int_in(sid, global_step_ids):
                errs.append(f"phases[{i}].supporting_step_ids[{j}]={sid!r} out of global")
        # sub_phases: 仅校验 step_range 两端为合法 int ∈ global(连续/覆盖由 presenter 确定性修补)
        for k, sp in enumerate(ph.get("sub_phases") or []):
            if isinstance(sp, dict):
                rng = sp.get("step_range") or []
                if not (isinstance(rng, list) and len(rng) == 2
                        and _is_int_in(rng[0], global_step_ids)
                        and _is_int_in(rng[1], global_step_ids)):
                    errs.append(f"phases[{i}].sub_phases[{k}].step_range={rng!r} invalid")
        for k, fs in enumerate(ph.get("failure_signals") or []):
            if isinstance(fs, dict):
                if fs.get("severity") not in {"low", "medium", "high"}:
                    errs.append(f"phases[{i}].failure_signals[{k}].severity invalid")
                for j, sid in enumerate(fs.get("step_ids") or []):
                    if not _is_int_in(sid, global_step_ids):
                        errs.append(f"phases[{i}].failure_signals[{k}].step_ids[{j}]={sid!r} out")
    return (len(errs) == 0), errs


def coerce_phase_summary(ps: PhaseSummary, global_step_ids: Set[int]) -> PhaseSummary:
    def _filter(ids):
        out = []
        for x in (ids or []):
            try:
                xi = int(x)
                if xi in global_step_ids:
                    out.append(xi)
            except (TypeError, ValueError):
                continue
        return out

    new_phases = []
    for ph in (ps.phases or []):
        if not isinstance(ph, dict):
            continue
        ph = dict(ph)
        ph["supporting_step_ids"] = _filter(ph.get("supporting_step_ids"))
        # sub_phases: 剔除 step_range 非法的项(连续/覆盖修补留给 presenter)
        new_sp = []
        for sp in (ph.get("sub_phases") or []):
            if isinstance(sp, dict):
                rng = sp.get("step_range") or []
                if (isinstance(rng, list) and len(rng) == 2
                        and rng[0] in global_step_ids and rng[1] in global_step_ids):
                    new_sp.append(dict(sp))
        ph["sub_phases"] = new_sp
        new_fs = []
        for fs in (ph.get("failure_signals") or []):
            if isinstance(fs, dict):
                fs = dict(fs)
                if fs.get("severity") not in {"low", "medium", "high"}:
                    fs["severity"] = "medium"
                fs["step_ids"] = _filter(fs.get("step_ids"))
                new_fs.append(fs)
        ph["failure_signals"] = new_fs
        new_phases.append(ph)
    ps.phases = new_phases
    return ps


# ----------------------------------------------------------------------------
# RootCauseAnnotation
# ----------------------------------------------------------------------------
def validate_root_cause(
    ann: RootCauseAnnotation,
    global_step_ids: Set[int],
    valid_agents_raw: Set[str],
    all_steps: Optional[List[Step]] = None,
) -> Tuple[bool, List[str]]:
    errs: List[str] = []

    # 1. step
    if not _step_field_ok(ann.step, global_step_ids):
        errs.append(f"step={ann.step!r} not a valid step_id and not 'system_evaluation'")

    # 2. agent
    if ann.agent not in valid_agents_raw and ann.agent not in config.SPECIAL_AGENTS:
        errs.append(f"agent={ann.agent!r} not in trajectory agents nor SPECIAL_AGENTS")

    # 3. evidence_step_ids
    if not isinstance(ann.evidence_step_ids, list):
        errs.append("evidence_step_ids not list")
    else:
        for i, sid in enumerate(ann.evidence_step_ids):
            if not _is_int_in(sid, global_step_ids):
                errs.append(f"evidence_step_ids[{i}]={sid!r} not in global step_ids")
        if not ann.abstain and not ann.evidence_step_ids:
            errs.append("evidence_step_ids empty while abstain=false")

    # 4. primary_category(单选,必填) + contributing_factors(可选多选)
    if not ann.primary_category:
        errs.append("primary_category empty (must pick exactly 1)")
    elif ann.primary_category not in config.CATEGORY_CODES:
        errs.append(f"primary_category={ann.primary_category!r} not in enum")
    if not isinstance(ann.contributing_factors, list):
        errs.append("contributing_factors not list")
    else:
        seen = set()
        for i, c in enumerate(ann.contributing_factors):
            if c not in config.CATEGORY_CODES:
                errs.append(f"contributing_factors[{i}]={c!r} not in enum")
            if c == ann.primary_category:
                errs.append(f"contributing_factors[{i}]={c!r} duplicates primary_category")
            if c in seen:
                errs.append(f"contributing_factors[{i}]={c!r} duplicated")
            seen.add(c)

    # 5. failure_chain
    da = ann.detailed_analysis if isinstance(ann.detailed_analysis, dict) else {}
    chain = da.get("failure_chain") if isinstance(da.get("failure_chain"), list) else []
    has_root = any(isinstance(x, dict) and x.get("role") == "root_cause" for x in chain)
    has_term = any(isinstance(x, dict) and x.get("role") == "terminal" for x in chain)
    if not has_root:
        errs.append("failure_chain missing role=root_cause node")
    if not has_term:
        errs.append("failure_chain missing role=terminal node")
    for i, node in enumerate(chain):
        if not isinstance(node, dict):
            errs.append(f"failure_chain[{i}] not dict")
            continue
        if node.get("role") not in config.ROLE_SET:
            errs.append(f"failure_chain[{i}].role={node.get('role')!r} not in enum")
        if not _step_field_ok(node.get("step", ""), global_step_ids):
            errs.append(f"failure_chain[{i}].step={node.get('step')!r} invalid")

    # 6. confidence
    conf = da.get("confidence", "")
    if conf not in config.CONFIDENCE_SET:
        errs.append(f"confidence={conf!r} not in enum")
    elif ann.abstain and conf != "low":
        errs.append(f"abstain=true requires confidence=low (got {conf})")

    # 7. planner 形态归因须有原文佐证(防反向过度归因到 DiagnostAgent / 委派态)。
    #    需要 all_steps 才能核对委派步原文;调用方未传则跳过(不破坏既有行为)。
    if all_steps is not None:
        errs.extend(_validate_planner_attribution(ann, da, all_steps))

    return (len(errs) == 0), errs


def _validate_planner_attribution(
    ann: RootCauseAnnotation, da: Dict[str, Any], all_steps: List[Step]
) -> List[str]:
    """把"根因归给 planner 任一形态"变成可机器校验的事实:必须指向真正的 planner 步,
    且(若是忠实实现回溯)能在该委派步原文里逐字找到被照搬的缺陷句。无标注下也成立——
    校验对象是轨迹自身文本。"""
    errs: List[str] = []
    step_by_id = {s.step_id: s for s in all_steps}
    planner_raw = {s.agent_name_raw for s in all_steps if s.agent_normalized == "planner"}

    dt = da.get("delegation_trace") if isinstance(da.get("delegation_trace"), dict) else {}
    faithful = bool(dt.get("is_faithful_implementation"))
    root_sid = _to_int(ann.step)
    root_step = step_by_id.get(root_sid) if root_sid is not None else None
    is_planner_attr = ann.agent in planner_raw

    # 7a. 忠实实现(trace-back)子情形:必须引用委派原文里的缺陷句,且字段一致。
    if faithful:
        pby = _to_int(dt.get("prescribed_by_step"))
        deleg = step_by_id.get(pby) if pby is not None else None
        if deleg is None or deleg.action_type != "delegate":
            errs.append(
                f"delegation_trace.prescribed_by_step={dt.get('prescribed_by_step')!r} 不是合法的 delegate 步"
            )
        else:
            if not _quote_grounded(str(dt.get("quote") or ""), deleg.content_full):
                errs.append(
                    f"delegation_trace.quote 未能在 delegate 步 {pby} 原文中找到"
                    f"(忠实实现回溯必须逐字引用被照搬的缺陷句,否则不准回溯到 planner)"
                )
            if root_sid != pby:
                errs.append(
                    f"is_faithful_implementation=true 时 step 必须=prescribed_by_step({pby}),实际={ann.step!r}"
                )
            # 委派态判定与委派记号形式解耦:名字内含 ' (-> '(名字编码式),
            # 或 agent == 该委派步的发起方名(字段/内容编码式)均可。
            deleg_form_ok = (" (-> " in ann.agent) or (ann.agent == deleg.agent_name_raw)
            if not deleg_form_ok:
                errs.append(
                    f"忠实实现回溯的 agent 须为委派步 {pby} 的发起方"
                    f"(委派态 'X (-> Y)' 或 ={deleg.agent_name_raw!r}),实际={ann.agent!r}"
                )
            if ann.primary_category not in _FAITHFUL_PRIMARY_OK:
                errs.append(
                    f"忠实实现回溯 primary_category 须 ∈ {{C1_flawed_plan, A2_ignored_constraint}},"
                    f"实际={ann.primary_category!r}(计划逻辑错→C1;计划无视任务明示方法/约束→A2)"
                )

    # 7b. 任何 planner 形态归因:根因须指向真正的 planner 步,且不得配执行/验证/非agent类主因。
    if is_planner_attr:
        if root_step is None or root_step.agent_normalized != "planner":
            errs.append(
                f"agent={ann.agent!r} 是 planner 形态,但 step={ann.step!r} 不是 planner 步"
                f"(不得把下游执行/验证错误默认甩给 planner)"
            )
        if ann.primary_category in _PLANNER_PRIMARY_FORBIDDEN:
            errs.append(
                f"planner 形态归因不得配执行/验证/非agent类主因(D*/E*/X*),实际={ann.primary_category!r}"
                f"(验证者漏检应为 E1_verification_gap contributing,不归 planner)"
            )

    return errs


def coerce_root_cause(
    ann: RootCauseAnnotation,
    global_step_ids: Set[int],
    valid_agents_raw: Set[str],
    all_steps: Optional[List[Step]] = None,
) -> RootCauseAnnotation:
    """温和修正越界。无法修正的字段保留原状,由上层用 abstain 兜底。"""
    if isinstance(ann.evidence_step_ids, list):
        ann.evidence_step_ids = [
            int(x) for x in ann.evidence_step_ids
            if isinstance(x, int) or (isinstance(x, str) and x.isdigit() and int(x) in global_step_ids)
        ]
        ann.evidence_step_ids = [x for x in ann.evidence_step_ids if x in global_step_ids]

    # primary_category 越界则清空(由上层用 abstain 兜底);contributing 剔除越界/重复/与主因重复
    if ann.primary_category and ann.primary_category not in config.CATEGORY_CODES:
        ann.primary_category = ""
    if isinstance(ann.contributing_factors, list):
        seen = set()
        ann.contributing_factors = [
            c for c in ann.contributing_factors
            if c in config.CATEGORY_CODES
            and c != ann.primary_category
            and not (c in seen or seen.add(c))
        ]
    else:
        ann.contributing_factors = []

    if ann.agent and ann.agent not in valid_agents_raw and ann.agent not in config.SPECIAL_AGENTS:
        ann.agent = "SYSTEM"

    if isinstance(ann.detailed_analysis, dict):
        conf = ann.detailed_analysis.get("confidence", "")
        if conf not in config.CONFIDENCE_SET:
            ann.detailed_analysis["confidence"] = "low"
        if ann.abstain:
            ann.detailed_analysis["confidence"] = "low"

        # 忠实实现回溯若无法被委派原文支撑,撤销该声明并交人工(不静默改 agent/step;
        # 避免 presenter 展示一条引不出原文的"委派溯源")。
        dt = ann.detailed_analysis.get("delegation_trace")
        if all_steps is not None and isinstance(dt, dict) and dt.get("is_faithful_implementation"):
            step_by_id = {s.step_id: s for s in all_steps}
            deleg = step_by_id.get(_to_int(dt.get("prescribed_by_step")))
            if (deleg is None or deleg.action_type != "delegate"
                    or not _quote_grounded(str(dt.get("quote") or ""), deleg.content_full)):
                dt["is_faithful_implementation"] = False
                ann.needs_human_review = True

    return ann
