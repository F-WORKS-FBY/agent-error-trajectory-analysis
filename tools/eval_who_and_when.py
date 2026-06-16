"""Who&When 评测:把流水线产出的 llm_mistake_agent/step 对照数据集自带的 ground truth。

Who&When(ICML'25)自带 who/when/why 标注(mistake_agent / mistake_step / mistake_reason),
是验证本方法**通用性**的天然标尺。本工具对一批已注入输出的 JSON 计算:
  - **agent 准确率**:llm_mistake_agent 是否命中 mistake_agent(忽略大小写、剥离委派后缀 ' (-> Y)')。
  - **step 准确率**:llm_mistake_step 是否 == mistake_step(整数,精确匹配)。
  - **agent&step 联合准确率**:两者同时命中。

注入输出里原始 mistake_* 与新加 llm_mistake_* 并存,故只需读输出目录即可。

用法:
  python -m MAS_trajectory_analysis.tools.eval_who_and_when --dir /path/to/output
  python -m MAS_trajectory_analysis.tools.eval_who_and_when --dir /path/to/output --json report.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_DELEG_SUFFIX = re.compile(r"\s*\(->\s*.+?\)\s*$")


def _norm_agent(name: Any) -> str:
    s = str(name or "").strip()
    s = _DELEG_SUFFIX.sub("", s)          # 'Orchestrator (-> WebSurfer)' -> 'Orchestrator'
    return s.lower()


def _deleg_target(name: Any) -> Optional[str]:
    m = re.search(r"\(->\s*(.+?)\)\s*$", str(name or ""))
    return m.group(1).strip().lower() if m else None


def _to_int(v: Any) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _agent_match(gt_agent: Any, llm_agent: Any) -> bool:
    """命中:裸名相等,或 ground-truth 名 == llm 的委派目标(委派态归因也算对那个接收方)。"""
    g, l = _norm_agent(gt_agent), _norm_agent(llm_agent)
    if not g:
        return False
    if g == l:
        return True
    tgt = _deleg_target(llm_agent)
    return tgt is not None and tgt == g


def evaluate_dir(out_dir: Path) -> Dict[str, Any]:
    files = sorted(
        p for p in out_dir.rglob("*.json") if not p.name.endswith(".debug.json")
    )
    rows: List[Dict[str, Any]] = []
    n_total = n_scored = n_agent = n_step = n_both = 0
    n_abstain = 0

    for p in files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if "mistake_agent" not in d and "mistake_step" not in d:
            continue                       # 非 Who&When 文件,跳过
        n_total += 1
        if "llm_mistake_agent" not in d:
            continue                       # 没跑出注入字段
        n_scored += 1

        gt_a, gt_s = d.get("mistake_agent"), _to_int(d.get("mistake_step"))
        llm_a, llm_s = d.get("llm_mistake_agent"), _to_int(d.get("llm_mistake_step"))
        las = d.get("llm_analysis_summary") or {}
        abstain = bool((las.get("root_cause") or {}).get("abstain"))
        if abstain:
            n_abstain += 1

        a_ok = _agent_match(gt_a, llm_a)
        s_ok = (gt_s is not None and llm_s is not None and gt_s == llm_s)
        n_agent += int(a_ok)
        n_step += int(s_ok)
        n_both += int(a_ok and s_ok)
        rows.append({
            "file": p.name, "gt_agent": gt_a, "gt_step": gt_s,
            "llm_agent": llm_a, "llm_step": llm_s,
            "agent_ok": a_ok, "step_ok": s_ok, "abstain": abstain,
        })

    def pct(n: int) -> float:
        return round(100.0 * n / n_scored, 1) if n_scored else 0.0

    return {
        "dir": str(out_dir),
        "n_files_with_gt": n_total,
        "n_scored": n_scored,
        "n_abstain": n_abstain,
        "agent_acc": pct(n_agent),
        "step_acc": pct(n_step),
        "agent_and_step_acc": pct(n_both),
        "rows": rows,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Who&When 评测(who/when 准确率 vs ground truth)")
    ap.add_argument("--dir", "-d", required=True, help="已注入输出的目录(递归扫描 *.json)")
    ap.add_argument("--json", default=None, help="把完整报告(含逐文件 rows)写到该 JSON 文件")
    ap.add_argument("--show-misses", action="store_true", help="打印判错的文件")
    args = ap.parse_args(argv)

    rep = evaluate_dir(Path(args.dir))
    print("=" * 70)
    print(f"Who&When eval | dir={rep['dir']}")
    print(f"  files with ground truth : {rep['n_files_with_gt']}")
    print(f"  scored (has llm output) : {rep['n_scored']}   (abstain: {rep['n_abstain']})")
    print(f"  agent accuracy          : {rep['agent_acc']}%")
    print(f"  step  accuracy          : {rep['step_acc']}%")
    print(f"  agent & step accuracy   : {rep['agent_and_step_acc']}%")
    print("=" * 70)
    if args.show_misses:
        for r in rep["rows"]:
            if not (r["agent_ok"] and r["step_ok"]):
                print(f"  [miss] {r['file']}: gt=({r['gt_agent']},{r['gt_step']}) "
                      f"llm=({r['llm_agent']},{r['llm_step']}) "
                      f"agent_ok={r['agent_ok']} step_ok={r['step_ok']}")
    if args.json:
        Path(args.json).write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
