"""标注平台展示层(Stage 5,纯函数,不调 LLM)。

把内部算好的"完整" summary_obj + RootCauseAnnotation + 原始 data 派生为
**精简的、纯展示型** `llm_analysis_summary`(平台只读这一块)。设计原则:
- 只保留要展示给标注员的内容;中间结果(segments/local_summaries/原始 phases/task_brief)
  不写进主输出(由 run.py 的 --debug-sidecar 单独落盘)。
- 根因只有一处 `root_cause`(最全),reason 只有一份完整解释,不再有 llm_suggestion 重复块。
- 每个 phase 内嵌 `sub_phases`(连续子段,确定性铺满该 phase,无断点)+ `anomaly_signals`(点事件)。
- 跨段矛盾放 `cross_phase_conflicts`。
所有 step_id 在写入前过滤为 ∈ 轨迹真实 step 集合。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .. import config
from ..core.schema import Step, RootCauseAnnotation


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def _clean_ids(ids: Any, valid: Set[int]) -> List[int]:
    out: List[int] = []
    seen: Set[int] = set()
    for x in (ids or []):
        try:
            xi = int(x)
        except (TypeError, ValueError):
            continue
        if xi in valid and xi not in seen:
            seen.add(xi)
            out.append(xi)
    return out


def _cat_meta(code: str) -> Dict[str, str]:
    m = config.CATEGORY_META.get(code or "", {})
    return {
        "code": code or "",
        "main": m.get("main", ""),
        "zh": m.get("zh", ""),
        "main_label": config.CATEGORY_MAIN_LABELS.get(m.get("main", ""), ""),
    }


_FAIL_PAT = re.compile(
    r"(FAILED\b|AssertionError|Traceback|Error:|ERROR\b|FAIL\b|assert\b|reward=0|✗)", re.IGNORECASE
)


def _extract_verdict_line(verifier_output: str, max_lines: int = 3, max_len: int = 400) -> str:
    if not verifier_output:
        return ""
    lines = [ln.strip() for ln in verifier_output.splitlines() if ln.strip()]
    hits = [ln for ln in lines if _FAIL_PAT.search(ln)]
    picked = hits[-max_lines:] if hits else lines[-max_lines:]
    return " | ".join(picked)[:max_len]


def _derive_bench_task(data: Dict[str, Any], src_name: str) -> Tuple[str, str]:
    """benchmark / task_name:metadata 优先 → ground_truth.benchmark → 文件名前缀。"""
    md = data.get("metadata") or {}
    gt = data.get("ground_truth")
    stem = re.sub(r"\.json$", "", src_name or "")
    benchmark = (
        md.get("benchmark")
        or (gt.get("benchmark") if isinstance(gt, dict) else None)
        or (stem.split("__")[0] if "__" in stem else "")
        or "?"
    )
    task_name = md.get("task_name") or data.get("question_ID") or stem or "?"
    return benchmark, task_name


def _brief(full_summary: Dict[str, Any], da: Dict[str, Any], data: Dict[str, Any]) -> str:
    ts = (da.get("task_summary") or "").strip()
    if ts:
        return ts[:500]
    q = (data.get("question") or "").strip()
    if q:
        return q[:500]
    tb = full_summary.get("task_brief") or ""
    m = re.search(r"question[^\n]*:\s*\n(.+?)(?:\n\nverifier_output|\Z)", tb, re.DOTALL)
    return (m.group(1) if m else tb).strip()[:500]


# ----------------------------------------------------------------------------
# sub_phases 确定性铺满修补(连续、不重叠、覆盖整个 phase)
# ----------------------------------------------------------------------------
def _tile_subphases(prange: List[int], raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 LLM 给的(可能有洞/重叠的)sub_phases 规整为连续铺满 [s,e] 的子段。"""
    if not (isinstance(prange, list) and len(prange) == 2):
        return []
    s, e = int(prange[0]), int(prange[1])
    if e < s:
        s, e = e, s
    # 收集合法 (start, end, desc),裁剪到 [s,e]
    items: List[Tuple[int, int, str]] = []
    for sp in (raw or []):
        if not isinstance(sp, dict):
            continue
        rng = sp.get("step_range") or []
        if not (isinstance(rng, list) and len(rng) == 2):
            continue
        try:
            a, b = int(rng[0]), int(rng[1])
        except (TypeError, ValueError):
            continue
        if b < a:
            a, b = b, a
        a, b = max(a, s), min(b, e)
        if a > b:
            continue
        items.append((a, b, (sp.get("description") or "").strip()))
    if not items:
        return [{"step_range": [s, e], "description": ""}]
    items.sort(key=lambda x: x[0])
    # 规整:首段从 s 起,逐段首尾相接,末段到 e
    tiled: List[Dict[str, Any]] = []
    cursor = s
    for i, (a, b, desc) in enumerate(items):
        start = cursor
        # 末段强制吃到 e;否则结束于本段 b(但不小于 start)
        if i == len(items) - 1:
            end = e
        else:
            nxt_start = items[i + 1][0]
            end = max(b, start)
            end = min(end, max(nxt_start - 1, start))
        end = min(max(end, start), e)
        tiled.append({"step_range": [start, end], "description": desc})
        cursor = end + 1
        if cursor > e:
            break
    # 若还没覆盖到 e,补一段
    if tiled and tiled[-1]["step_range"][1] < e:
        tiled[-1]["step_range"][1] = e
    return tiled


# ----------------------------------------------------------------------------
# anomaly 归属到 phase
# ----------------------------------------------------------------------------
def _phase_index_for(step_ids: List[int], phase_ranges: List[Tuple[int, List[int]]]) -> Optional[int]:
    """返回包含(首个)step 的 phase_id;找不到返回 None。"""
    if not step_ids:
        return None
    sid = step_ids[0]
    for pid, rng in phase_ranges:
        if len(rng) == 2 and rng[0] <= sid <= rng[1]:
            return pid
    return None


# ----------------------------------------------------------------------------
# phases(内嵌 sub_phases + anomaly_signals)
# ----------------------------------------------------------------------------
def _build_phases(full_summary: Dict[str, Any], valid: Set[int]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    phases_block = full_summary.get("phases") or {}
    raw_phases = phases_block.get("phases") or []
    local_summaries = full_summary.get("local_summaries") or []

    phases: List[Dict[str, Any]] = []
    phase_ranges: List[Tuple[int, List[int]]] = []
    for ph in raw_phases:
        if not isinstance(ph, dict):
            continue
        pid = ph.get("phase_id")
        rng = ph.get("step_range") or []
        phase_ranges.append((pid, rng))
        anomalies: List[Dict[str, Any]] = []
        seen_desc: Set[str] = set()
        # phase 自带的 failure_signals(点事件)
        for fs in (ph.get("failure_signals") or []):
            if isinstance(fs, dict):
                d = (fs.get("description") or "").strip()
                if d and d not in seen_desc:
                    seen_desc.add(d)
                    anomalies.append({
                        "kind": "failure_signal", "description": d,
                        "step_ids": _clean_ids(fs.get("step_ids"), valid),
                        "severity": fs.get("severity"),
                    })
        phases.append({
            "phase_id": pid,
            "step_range": rng,
            "phase_goal": ph.get("phase_goal") or "",
            "involved_agents": ph.get("involved_agents") or [],
            "sub_phases": _tile_subphases(rng, ph.get("sub_phases") or []),
            "anomaly_signals": anomalies,
            "_seen": seen_desc,   # 临时,稍后删
        })

    # 把 local 的 candidate_failures(type!=none)归属到对应 phase
    by_id = {p["phase_id"]: p for p in phases}
    for ls in local_summaries:
        for cf in (ls.get("candidate_failures") or []):
            if not isinstance(cf, dict) or cf.get("type") in (None, "", "none"):
                continue
            sids = _clean_ids(cf.get("step_ids"), valid)
            pid = _phase_index_for(sids, phase_ranges)
            tgt = by_id.get(pid)
            if tgt is None:
                continue
            d = (cf.get("why") or "").strip()
            if d and d not in tgt["_seen"]:
                tgt["_seen"].add(d)
                tgt["anomaly_signals"].append({
                    "kind": "candidate_failure", "description": d,
                    "step_ids": sids, "severity": None,
                })
    for p in phases:
        p.pop("_seen", None)

    # 跨段矛盾
    conflicts = []
    for cf in (phases_block.get("conflicts") or []):
        if isinstance(cf, dict):
            conflicts.append({
                "description": (cf.get("description") or "").strip(),
                "step_ids": _clean_ids(cf.get("step_ids"), valid),
            })
    return phases, conflicts


# ----------------------------------------------------------------------------
# root_cause(唯一,最全,单 reason)
# ----------------------------------------------------------------------------
def _build_root_cause(ann: RootCauseAnnotation, da: Dict[str, Any], valid: Set[int]) -> Dict[str, Any]:
    chain = []
    for node in (da.get("failure_chain") or []):
        if isinstance(node, dict):
            chain.append({
                "step": str(node.get("step", "")),
                "agent": node.get("agent", ""),
                "role": node.get("role", ""),
                "description": node.get("description", ""),
            })
    rc: Dict[str, Any] = {
        "agent": ann.agent or "",
        "step": str(ann.step or ""),
        "primary_category": _cat_meta(ann.primary_category),
        "contributing_factors": [_cat_meta(c) for c in (ann.contributing_factors or [])],
        "reason": ann.reason or "",                     # 唯一完整解释
        "failure_chain": chain,
        "confidence": da.get("confidence", ""),
        "confidence_reason": da.get("confidence_reason", ""),
        "evidence_step_ids": _clean_ids(ann.evidence_step_ids, valid),
        "counterfactual": da.get("counterfactual", ""),
        "expert_review_hints": list(da.get("expert_review_hints") or []),
        "abstain": bool(ann.abstain),
        "needs_human_review": bool(ann.needs_human_review),
        "banner": ("LLM 对该轨迹根因不确定,请以人工判定为准"
                   if (ann.abstain or ann.needs_human_review) else ""),
    }
    return rc


def _category_legend(rc: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    legend: Dict[str, Dict[str, str]] = {}
    for c in [rc["primary_category"]] + list(rc["contributing_factors"]):
        code = c.get("code")
        if code:
            legend[code] = {"zh": c.get("zh", ""), "main": c.get("main", ""),
                            "main_label": c.get("main_label", "")}
    return legend


# ----------------------------------------------------------------------------
# step_ref_index(防漂移锚定)
# ----------------------------------------------------------------------------
def _build_step_ref_index(phases: List[Dict[str, Any]], rc: Dict[str, Any],
                          step_by_id: Dict[int, Step]) -> Dict[str, Dict[str, Any]]:
    ids: Set[int] = set()
    for p in phases:
        for end in (p.get("step_range") or []):
            if isinstance(end, int):
                ids.add(end)
        for sp in p.get("sub_phases", []):
            for end in sp.get("step_range", []):
                if isinstance(end, int):
                    ids.add(end)
        for a in p.get("anomaly_signals", []):
            ids.update(a.get("step_ids", []))
    ids.update(rc.get("evidence_step_ids", []))
    for n in rc.get("failure_chain", []):
        if str(n.get("step", "")).isdigit():
            ids.add(int(n["step"]))
    idx: Dict[str, Dict[str, Any]] = {}
    for sid in sorted(ids):
        st = step_by_id.get(sid)
        if st is not None:
            idx[str(sid)] = {"step_hash": st.step_hash, "agent": st.agent_name_raw,
                             "action_type": st.action_type}
    return idx


# ----------------------------------------------------------------------------
# Markdown 兜底
# ----------------------------------------------------------------------------
def _chips(ids: List[int]) -> str:
    return " ".join(f"[#{i}]" for i in ids) if ids else "—"


def _rng(r: List[int]) -> str:
    return f"[#{r[0]}–#{r[1]}]" if isinstance(r, list) and len(r) == 2 else ""


def _render_ai_md(meta: Dict[str, Any], tg: Dict[str, Any], phases: List[Dict[str, Any]],
                  conflicts: List[Dict[str, Any]]) -> str:
    verdict = "✅ 成功" if tg.get("is_correct") else "❌ 失败"
    head = " · ".join(x for x in [meta.get("task_name"), meta.get("benchmark"), meta.get("model")] if x)
    L = [f"### 任务目标  {verdict}"]
    if head:
        L.append(f"`{head}`")
    if tg.get("brief"):
        L.append(tg["brief"])
    if tg.get("verdict_line"):
        L.append(f"> verifier: {tg['verdict_line']}")
    L.append("\n### 阶段时间线(点开看每段子步与异常信号)")
    mark = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    for p in phases:
        L.append(f"\n**P{p['phase_id']} {_rng(p['step_range'])} {p['phase_goal']}**"
                 + (f"  ({', '.join(p['involved_agents'])})" if p.get("involved_agents") else ""))
        for sp in p.get("sub_phases", []):
            L.append(f"- {_rng(sp['step_range'])} {sp['description']}")
        for a in p.get("anomaly_signals", []):
            sev = mark.get(a.get("severity") or "", "•")
            L.append(f"  - {sev} [{a['kind']}] {a['description']}  {_chips(a['step_ids'])}".rstrip())
    if conflicts:
        L.append("\n### 跨段矛盾")
        for c in conflicts:
            L.append(f"- 🔴 {c['description']}  {_chips(c['step_ids'])}")
    return "\n".join(L)


def _render_root_cause_md(rc: Dict[str, Any]) -> str:
    L = ["### 根因(LLM 建议,仅参考,不自动填入)"]
    if rc.get("banner"):
        L.append(f"> ⚠️ {rc['banner']}")
    pc = rc["primary_category"]
    pc_s = pc["code"] + (f"({pc['zh']})" if pc.get("zh") else "")
    cf_s = ", ".join(f"{c['code']}({c['zh']})" if c.get("zh") else c["code"]
                     for c in rc["contributing_factors"]) or "—"
    step = f"[#{rc['step']}]" if str(rc['step']).isdigit() else rc['step']
    L.append(f"- **责任 agent**: {rc['agent']}    **决定性 step**: {step}")
    L.append(f"- **主因**: {pc_s}    **次因**: {cf_s}")
    L.append(f"- **原因**: {rc['reason']}")
    if rc.get("failure_chain"):
        L.append("- **失败链**(区分最初根因 vs 失败显露处):")
        role_zh = {"root_cause": "根因", "propagation": "传播", "exposure": "显露", "terminal": "终态"}
        for i, n in enumerate(rc["failure_chain"], 1):
            st = f"[#{n['step']}]" if str(n['step']).isdigit() else n['step']
            L.append(f"  {i}. {st} `{role_zh.get(n['role'], n['role'])}` {n.get('agent','')} — {n.get('description','')}")
    L.append(f"- **置信度**: {rc.get('confidence','')}"
             + (f"  ({rc['confidence_reason']})" if rc.get("confidence_reason") else ""))
    if rc.get("evidence_step_ids"):
        L.append(f"- **证据**: {_chips(rc['evidence_step_ids'])}")
    if rc.get("counterfactual"):
        L.append(f"- **反事实**: {rc['counterfactual']}")
    if rc.get("expert_review_hints"):
        L.append("- **人工核验提示**:")
        for h in rc["expert_review_hints"]:
            L.append(f"  - ☐ {h}")
    return "\n".join(L)


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
def build_lean_summary(
    full_summary: Dict[str, Any],
    ann: RootCauseAnnotation,
    data: Dict[str, Any],
    steps: List[Step],
    src_name: str,
) -> Dict[str, Any]:
    """从完整 summary 派生精简的、纯展示型 llm_analysis_summary(平台只读这一块)。"""
    valid: Set[int] = {s.step_id for s in steps}
    step_by_id: Dict[int, Step] = {s.step_id: s for s in steps}
    da = ann.detailed_analysis if isinstance(ann.detailed_analysis, dict) else {}
    benchmark, task_name = _derive_bench_task(data, src_name)

    meta = {
        "schema_version": full_summary.get("schema_version"),
        "timestamp": full_summary.get("timestamp"),
        "model": full_summary.get("model"),
        "benchmark": benchmark,
        "task_name": task_name,
        "n_steps": full_summary.get("n_steps"),
        "n_phases": full_summary.get("n_phases"),
    }
    task_goal = {
        "is_correct": bool(data.get("is_correct", False)),
        "brief": _brief(full_summary, da, data),
        "verdict_line": _extract_verdict_line(data.get("verifier_output") or ""),
    }
    phases, conflicts = _build_phases(full_summary, valid)
    root_cause = _build_root_cause(ann, da, valid)

    lean: Dict[str, Any] = {
        "meta": meta,
        "task_goal": task_goal,
        "phases": phases,
        "cross_phase_conflicts": conflicts,
        "root_cause": root_cause,
        "category_legend": _category_legend(root_cause),
        "step_ref_index": _build_step_ref_index(phases, root_cause, step_by_id),
    }
    lean["ai_summary_markdown"] = _render_ai_md(meta, task_goal, phases, conflicts)
    lean["root_cause_markdown"] = _render_root_cause_md(root_cause)
    return lean
