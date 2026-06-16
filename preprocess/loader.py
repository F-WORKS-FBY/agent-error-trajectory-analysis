"""轨迹文件读取 + bench 文件列表枚举。

输入是 `Who&When_style/<bench>/*.json` 顶层文件,history 字段是 step 数组。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from .. import config
from ..core.schema import Step
from .step_enricher import enrich_steps
from .profile import DatasetProfile, DEFAULT_PROFILE, resolve_profile


def list_bench_files(bench: str) -> List[Path]:
    """(兼容旧调用)列出 内置 Who&When_style/<bench> 目录下的所有 *.json。"""
    return list_input_files(config.WHO_WHEN_DIR, bench)


def list_input_files(input_dir: Path, benchmark: Optional[str] = None) -> List[Path]:
    """列出待处理文件(按文件名稳定排序),忽略 *.debug.json。

    - 给了 benchmark: 读 <input_dir>/<benchmark>/*.json(经典布局)。
    - 没给 benchmark: 读 <input_dir>/*.json(扁平布局,如他人自带的 bench 目录)。
    """
    base = (input_dir / benchmark) if benchmark else input_dir
    if not base.exists():
        raise FileNotFoundError(f"input dir not found: {base}")
    return sorted(
        p for p in base.glob("*.json")
        if p.is_file() and not p.name.endswith(".debug.json")
    )


def load_raw(src_path: Path) -> Dict[str, Any]:
    """读原始 JSON,返回 dict(完整保留所有字段)。"""
    with src_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_trajectory(
    src_path: Path,
    profile: Optional[DatasetProfile] = None,
) -> Tuple[Dict[str, Any], List[Step], DatasetProfile]:
    """读原始 JSON 并将 history 经启发式抽取转成 Step 列表。

    profile=None 时按数据内容嗅探兜底。返回 (data, steps, 实际使用的 profile)。
    """
    data = load_raw(src_path)
    prof = profile if profile is not None else resolve_profile(None, data)
    history = data.get(prof.history_key) or []
    steps = enrich_steps(history, prof)
    return data, steps, prof


def build_task_brief(
    data: Dict[str, Any],
    src_name: str = "",
    profile: DatasetProfile = DEFAULT_PROFILE,
) -> str:
    """生成给 LLM 看的任务概况(用于所有三个 prompt)。

    字段名经 profile 解析,兼容缺 metadata / 缺 verifier_output 的格式(如 magentic_one / Who&When):
    - benchmark 回退到 ground_truth.benchmark 或文件名前缀;
    - ground_truth 为 dict 时也注入其期望/约束。
    """
    parts = []
    md = data.get(profile.metadata_field) or {}
    if not isinstance(md, dict):
        md = {}
    gt = data.get(profile.ground_truth_field)
    stem = (src_name or "").removesuffix(".json")
    bench = (
        md.get("benchmark")
        or (gt.get("benchmark") if isinstance(gt, dict) else None)
        or (stem.split("__")[0] if "__" in stem else "?")
    )
    task = md.get("task_name") or data.get("question_ID") or stem or "?"
    parts.append(f"benchmark: {bench}")
    parts.append(f"task: {task}")
    parts.append(f"is_correct: {data.get(profile.is_correct_field)}")

    # Round 6:不裁内容,完整发(百万上下文足够)。决定性的任务要求/示例/接口规范常在 question 后段。
    q = data.get(profile.question_field) or ""
    if isinstance(q, str) and q:
        parts.append(f"question (完整):\n{q}")

    vo = data.get(profile.verifier_field) or ""
    if isinstance(vo, str) and vo:
        parts.append(f"verifier_output (完整):\n{vo}")

    rt = data.get(profile.runtime_errors_field)
    if rt:
        parts.append(f"runtime_errors: {json.dumps(rt, ensure_ascii=False)}")

    if isinstance(gt, str) and gt:
        parts.append(f"ground_truth (完整):\n{gt}")
    elif isinstance(gt, dict) and gt:
        parts.append(f"ground_truth (期望/约束):\n{json.dumps(gt, ensure_ascii=False)}")

    # agent_patch: the agent's final consolidated diff (a convenient single view of
    # what it actually changed; the per-step edits also live inside history). Lets
    # the LLM diff the agent's solution against ground_truth for root-cause analysis.
    ap = data.get(profile.agent_patch_field)
    if isinstance(ap, str) and ap.strip():
        parts.append(f"agent_patch (agent 最终提交的代码改动 diff):\n{ap}")

    return "\n\n".join(parts)
