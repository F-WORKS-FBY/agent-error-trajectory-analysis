"""把原 JSON 完整拷贝后,在顶层注入 5 个 v2 字段;原子写;文件锁。

布局:
- 源:   Who&When_style/<bench>/<filename>.json   ← 不动
- 输出: Who&When_style/MAS_trajectory_analysis/<bench>/<filename>.json   ← 拷贝 + 注入
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .. import config

LOG = logging.getLogger("MAS_trajectory_analysis.io")

_FILE_LOCKS: Dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _get_file_lock(path: str) -> threading.Lock:
    with _FILE_LOCKS_GUARD:
        if path not in _FILE_LOCKS:
            _FILE_LOCKS[path] = threading.Lock()
        return _FILE_LOCKS[path]


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _step_to_int(step_str: Union[str, int]) -> int:
    """LLM 输出的 `step` 可能是 "186" / "system_evaluation" / int 等。"""
    if isinstance(step_str, int):
        return step_str
    s = str(step_str or "").strip()
    if not s:
        return config.SYSTEM_EVAL_STEP_SENTINEL
    if s.lower() in config.ALLOWED_PSEUDO_STEPS:
        return config.SYSTEM_EVAL_STEP_SENTINEL
    try:
        return int(s)
    except ValueError:
        return config.SYSTEM_EVAL_STEP_SENTINEL


def inject_v2_fields(
    original: Dict[str, Any],
    llm_mistake_agent: str,
    llm_mistake_step: Union[int, str],
    llm_mistake_reason: str,
    llm_analysis_summary: Dict[str, Any],
) -> Dict[str, Any]:
    """深拷贝原 JSON,在顶层注入/覆盖 4 个字段(其余保持不变)。

    顶层 llm_mistake_agent/step/reason 精确镜像原始 Who&When 的 mistake_* 三槽;
    类别(primary_category / contributing_factors)只存在于
    llm_analysis_summary.root_cause,不再在顶层重复。
    """
    out = copy.deepcopy(original)
    if isinstance(llm_mistake_step, str):
        llm_mistake_step = _step_to_int(llm_mistake_step)
    out["llm_mistake_agent"] = llm_mistake_agent
    out["llm_mistake_step"] = llm_mistake_step
    out["llm_mistake_reason"] = llm_mistake_reason
    out["llm_analysis_summary"] = llm_analysis_summary
    return out


def write_v2_result(
    out_root: Path,
    bench: str,
    filename: str,
    injected: Dict[str, Any],
) -> Path:
    """写入 out_root/<bench>/<filename>(原子 + 文件锁)。"""
    out_dir = out_root / bench
    out_path = out_dir / filename
    with _get_file_lock(str(out_path)):
        _atomic_write_json(out_path, injected)
    return out_path


def write_debug_sidecar(
    out_root: Path,
    bench: str,
    filename: str,
    debug_obj: Dict[str, Any],
) -> Path:
    """把完整中间结果写到 <out_root>/<bench>/<name>.debug.json(审计用,不进主输出)。"""
    base = filename[:-5] if filename.endswith(".json") else filename
    out_dir = out_root / bench
    out_path = out_dir / f"{base}.debug.json"
    with _get_file_lock(str(out_path)):
        _atomic_write_json(out_path, debug_obj)
    return out_path


def out_path_for(bench: str, filename: str, out_root: Optional[Path] = None) -> Path:
    root = out_root or config.OUT_ROOT
    return root / bench / filename


def is_complete_v2(out_path: Path) -> bool:
    """判断 v2 输出文件是否已成功生成(用于 resume)。"""
    if not out_path.exists():
        return False
    try:
        with out_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    if "llm_analysis_summary" not in data:
        return False
    summary = data["llm_analysis_summary"]
    if not isinstance(summary, dict):
        return False
    # 至少有 root_cause 字段
    return isinstance(summary.get("root_cause"), dict)
