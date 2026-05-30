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


def load_trajectory(src_path: Path) -> Tuple[Dict[str, Any], List[Step]]:
    """读原始 JSON 并将 history 经启发式抽取转成 Step 列表。"""
    data = load_raw(src_path)
    history = data.get("history") or []
    steps = enrich_steps(history)
    return data, steps


def build_task_brief(data: Dict[str, Any], src_name: str = "") -> str:
    """生成给 LLM 看的任务概况(用于所有三个 prompt)。

    兼容缺 metadata / 缺 verifier_output 的格式(如 magentic_one):
    - benchmark 回退到 ground_truth.benchmark 或文件名前缀;
    - ground_truth 为 dict 时也注入其期望/约束。
    """
    parts = []
    md = data.get("metadata") or {}
    gt = data.get("ground_truth")
    stem = (src_name or "").removesuffix(".json")
    bench = (
        md.get("benchmark")
        or (gt.get("benchmark") if isinstance(gt, dict) else None)
        or (stem.split("__")[0] if "__" in stem else "?")
    )
    task = md.get("task_name") or data.get("question_ID") or stem or "?"
    parts.append(f"benchmark: {bench}")
    parts.append(f"task: {task}")
    parts.append(f"is_correct: {data.get('is_correct')}")

    q = data.get("question") or ""
    if isinstance(q, str) and q:
        parts.append(f"question (head {config.TASK_QUESTION_CHARS} chars):\n{q[:config.TASK_QUESTION_CHARS]}")

    vo = data.get("verifier_output") or ""
    if isinstance(vo, str) and vo:
        parts.append(
            f"verifier_output (head {config.TASK_VERIFIER_CHARS} chars):\n"
            f"{vo[:config.TASK_VERIFIER_CHARS]}"
        )

    rt = data.get("runtime_errors")
    if rt:
        parts.append(f"runtime_errors: {json.dumps(rt, ensure_ascii=False)[:1500]}")

    if isinstance(gt, str) and gt:
        parts.append(f"ground_truth (head 1000 chars):\n{gt[:1000]}")
    elif isinstance(gt, dict) and gt:
        parts.append(f"ground_truth (期望/约束):\n{json.dumps(gt, ensure_ascii=False)[:1500]}")

    return "\n\n".join(parts)
