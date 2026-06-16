"""按-stage thinking 配置的基准:在 4 个锚点轨迹上跑当前(新)配置,
对照已知正确根因验证不回退,并报告每 stage 的返回速度 + token 消耗。

用法(从 scripts/ 父目录):
  python -m MAS_trajectory_analysis.tools.bench_thinking
  python -m MAS_trajectory_analysis.tools.bench_thinking --files instance_ansible__ansible-185d41__5d9cizL.json   # 冒烟:只跑最小文件

说明:
  - 只跑**当前 config 的配置**(新:local 关 thinking / phase high / root max),不做旧基线 A/B。
  - 每个文件按 stage 聚合 latency + token(local/phase/root),并把根因判定与锚点对照。
  - 输出写到专用 scratch 目录(默认 Who&When_style/_thinking_bench),**不污染**生产输出。
  - token/latency 只进本报告,不写进 v2 输出(输出契约不变)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional

from .. import config
from ..core.llm_client import DeepSeekClient
from ..output.io_writer import out_path_for
from ..run import process_one


# 锚点:文件名 -> 期望根因(权威:人工标注 + 独立判定)。category 用集合(允许等价类)。
ANCHORS: Dict[str, Dict[str, Any]] = {
    "instance_ansible__ansible-11c177__7EWxoo8.json": {
        "agent_contains": "(-> ActionAgent)", "step": 21,
        "category": {"A2_ignored_constraint", "C1_flawed_plan"}, "faithful": True,
        "note": "planner 选错数据源(任务要 routing table,委派用 ip addr show)→ 回溯 planner",
    },
    "instance_NodeBB__NodeBB-9c576a07__toJzMiS.json": {
        "agent_contains": "(-> ActionAgent)", "step": 27,
        "category": {"C1_flawed_plan"}, "faithful": True,
        "note": "email 校验顺序错(结构型计划缺陷)→ 回溯 planner",
    },
    "instance_ansible__ansible-185d41__5d9cizL.json": {
        "agent_contains": "(-> ActionAgent)", "step": 23,
        "category": {"C1_flawed_plan"}, "faithful": True,
        "note": "不完整/缺漏型计划缺陷 → 回溯 planner",
    },
    "instance_internetarchive__openli__BtBcqzL.json": {
        "agent_contains": "", "step": 0,        # X1 责任 agent 浮动(human/USER_INTENT_UNDERSPECIFIED),不硬卡;主门禁看 category+faithful
        "category": {"X1_underspecified_input"}, "faithful": False,
        "note": "任务无解:需求10 强制 published_in_future_year(delta)->delta>0,但官方免改测试 "
                "test_published_in_future_year 仍传绝对年份、期望旧语义,二者不可兼得且禁改测试 "
                "→ X1(矛盾在 step 0 任务书即存在,非 agent 引入)",
    },
}

_TOKEN_KEYS = ["prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens"]
_STAGE_ORDER = ["local", "phase", "root"]


def _aggregate_by_stage(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """把一批 per-call 记录按 stage 聚合:n_calls、Σlatency、Σ各类 token(None 记 missing)。"""
    agg: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        st = rec.get("stage") or "?"
        slot = agg.setdefault(st, {"n_calls": 0, "latency_s": 0.0,
                                   **{k: 0 for k in _TOKEN_KEYS},
                                   **{f"{k}_missing": 0 for k in _TOKEN_KEYS}})
        slot["n_calls"] += 1
        slot["latency_s"] += float(rec.get("latency_s") or 0.0)
        for k in _TOKEN_KEYS:
            v = rec.get(k)
            if v is None:
                slot[f"{k}_missing"] += 1
            else:
                slot[k] += int(v)
    return agg


def _fmt_tok(slot: Dict[str, Any], key: str) -> str:
    # 该 stage 所有调用该 token 字段都缺 → "—";否则给和(带缺失标记)。
    if slot[f"{key}_missing"] >= slot["n_calls"]:
        return "—"
    suffix = f"*{slot['n_calls'] - slot[f'{key}_missing']}/{slot['n_calls']}" if slot[f"{key}_missing"] else ""
    return f"{slot[key]}{suffix}"


def _print_stage_table(fname: str, agg: Dict[str, Dict[str, Any]]) -> None:
    print(f"\n  每-stage 成本({fname}):")
    hdr = f"    {'stage':<7} {'calls':>5} {'latency_s':>10} {'prompt':>9} {'completion':>11} {'reasoning':>10} {'total':>9}"
    print(hdr)
    print("    " + "-" * (len(hdr) - 4))
    tot_lat, tot = 0.0, {k: 0 for k in _TOKEN_KEYS}
    for st in _STAGE_ORDER + [s for s in agg if s not in _STAGE_ORDER]:
        if st not in agg:
            continue
        slot = agg[st]
        tot_lat += slot["latency_s"]
        for k in _TOKEN_KEYS:
            tot[k] += slot[k]
        print(f"    {st:<7} {slot['n_calls']:>5} {slot['latency_s']:>10.1f} "
              f"{_fmt_tok(slot,'prompt_tokens'):>9} {_fmt_tok(slot,'completion_tokens'):>11} "
              f"{_fmt_tok(slot,'reasoning_tokens'):>10} {_fmt_tok(slot,'total_tokens'):>9}")
    print(f"    {'TOTAL':<7} {'':>5} {tot_lat:>10.1f} {tot['prompt_tokens']:>9} "
          f"{tot['completion_tokens']:>11} {tot['reasoning_tokens']:>10} {tot['total_tokens']:>9}")


def _verdict(fname: str, rc: Dict[str, Any]) -> Dict[str, Any]:
    exp = ANCHORS.get(fname, {})
    agent = rc.get("agent", "")
    step = str(rc.get("step", ""))
    cat = rc.get("primary_category", "")
    faithful = bool((rc.get("detailed_analysis") or {})
                    .get("delegation_trace", {}).get("is_faithful_implementation"))
    cat_ok = cat in exp.get("category", set())
    agent_ok = exp.get("agent_contains", "") in agent
    step_ok = step == str(exp.get("step", "")) if exp.get("step") is not None else None
    return {
        "agent": agent, "step": step, "primary_category": cat, "faithful": faithful,
        "expected": {"agent_contains": exp.get("agent_contains"), "step": exp.get("step"),
                     "category": sorted(exp.get("category", [])), "faithful": exp.get("faithful")},
        "cat_ok": cat_ok, "agent_ok": agent_ok, "step_ok": step_ok,
        "note": exp.get("note", ""),
    }


def _read_root_cause(out_root: Path, fname: str) -> Optional[Dict[str, Any]]:
    """从 debug sidecar 读 full_summary.root_cause(含 primary_category / delegation_trace)。"""
    base = fname[:-5] if fname.endswith(".json") else fname
    dbg = out_root / "swe_bench_pro" / f"{base}.debug.json"
    if not dbg.exists():
        return None
    try:
        obj = json.loads(dbg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return (obj.get("full_summary") or {}).get("root_cause")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="按-stage thinking 配置基准(4 锚点 + 每-stage 成本)")
    ap.add_argument("--data-dir", default="/volume/coder/users/yzli02/swwu/jincheng/fengboyu/CNIC/zhangyunfei/data",
                    help="数据根(其下 swe_bench_pro/*.json)")
    ap.add_argument("--out-dir", default=str(config.WHO_WHEN_DIR / "_thinking_bench"),
                    help="scratch 输出目录(不污染生产)")
    ap.add_argument("--files", nargs="*", default=list(ANCHORS.keys()),
                    help="要跑的锚点文件名(默认 4 个)")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    out_root = Path(args.out_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("按-stage thinking 配置基准 | 当前 config:")
    print(f"  local : thinking={config.LLM_THINKING_LOCAL}  effort={config.LLM_REASONING_EFFORT_LOCAL}")
    print(f"  phase : thinking={config.LLM_THINKING_PHASE}  effort={config.LLM_REASONING_EFFORT_PHASE}")
    print(f"  root  : thinking={config.LLM_THINKING_ROOT}  effort={config.LLM_REASONING_EFFORT_ROOT}")
    print(f"  model={config.LLM_MODEL}  out={out_root}")
    print("=" * 78)

    try:
        client = DeepSeekClient(model=config.LLM_MODEL, base_url=config.LLM_BASE_URL)
    except RuntimeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    # 本基准只测单一配置模型 → 三个阶段共用同一个 client(metrics_sink 汇总全阶段)。
    stage_clients = {"local": client, "phase": client, "root": client}

    report: Dict[str, Any] = {"config": {
        "local": {"thinking": config.LLM_THINKING_LOCAL, "effort": config.LLM_REASONING_EFFORT_LOCAL},
        "phase": {"thinking": config.LLM_THINKING_PHASE, "effort": config.LLM_REASONING_EFFORT_PHASE},
        "root": {"thinking": config.LLM_THINKING_ROOT, "effort": config.LLM_REASONING_EFFORT_ROOT},
        "model": config.LLM_MODEL,
    }, "files": {}}

    for fname in args.files:
        src = data_dir / "swe_bench_pro" / fname
        print(f"\n{'#'*78}\n# {fname}")
        if not src.exists():
            print(f"  [SKIP] 文件不存在: {src}", file=sys.stderr)
            continue
        client.metrics_sink = []           # 每文件重置
        try:
            status = process_one(src, "swe_bench_pro", stage_clients, out_root,
                                 overwrite=True, dry_run=False, debug_sidecar=True)
        except Exception as e:             # noqa: BLE001 — 基准要继续跑下一个
            print(f"  [ERROR] process_one 失败: {type(e).__name__}: {e}", file=sys.stderr)
            continue

        records = list(client.metrics_sink or [])
        agg = _aggregate_by_stage(records)
        _print_stage_table(fname, agg)

        rc = _read_root_cause(out_root, fname)
        if rc is None:
            print("  [WARN] 读不到 root_cause(debug sidecar 缺失)", file=sys.stderr)
            report["files"][fname] = {"status": status, "stages": agg, "verdict": None}
            continue
        v = _verdict(fname, rc)
        cat_mark = "✓" if v["cat_ok"] else "✗"
        agent_mark = "✓" if v["agent_ok"] else "✗"
        step_mark = "—" if v["step_ok"] is None else ("✓" if v["step_ok"] else "✗")
        print(f"\n  判定 vs 锚点  (status={status})")
        print(f"    实际:  agent={v['agent']!r}  step={v['step']}  category={v['primary_category']}  faithful={v['faithful']}")
        print(f"    期望:  agent⊃{v['expected']['agent_contains']!r}  step={v['expected']['step']}  "
              f"category∈{v['expected']['category']}  faithful={v['expected']['faithful']}")
        print(f"    匹配:  category {cat_mark}   agent {agent_mark}   step {step_mark}   | {v['note']}")
        report["files"][fname] = {"status": status, "stages": agg, "verdict": v}

    report_path = out_root / "bench_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'='*78}\n报告已写: {report_path}")

    # 汇总
    n = sum(1 for f in report["files"].values() if f.get("verdict"))
    cat_pass = sum(1 for f in report["files"].values() if (f.get("verdict") or {}).get("cat_ok"))
    print(f"category 不回退: {cat_pass}/{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
