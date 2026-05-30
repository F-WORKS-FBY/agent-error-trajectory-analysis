"""把原始 history dict 列表转成 Step。

负责:
- normalize_agent_name (按 5 bench 通用规则归一)
- action_type 决策树
- verifier_signal / exit_code / success 启发式
- step_hash 指纹
"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, Any, List, Optional

from .. import config
from ..core.schema import Step


# ----------------------------------------------------------------------------
# Agent normalization
# ----------------------------------------------------------------------------
PLANNERS = {"DiagnostAgent", "Task_Planner"}
EXECUTORS = {"ActionAgent", "Action_Expert"}
VERIFIERS = {"JudgeAgent", "Verification_Expert"}
TERMINAL_NAMES = {"Computer_terminal"}
HUMAN_NAMES = {"human"}


def normalize_agent_name(name: str) -> str:
    n = (name or "").strip()
    if not n:
        return "unknown"
    # "X (-> Y)" 视为 X(委派的发起方)
    base = n.split(" (-> ")[0].strip()
    if base in PLANNERS:
        return "planner"
    if base in EXECUTORS:
        return "executor"
    if base in VERIFIERS:
        return "verifier"
    if base in TERMINAL_NAMES:
        return "terminal"
    if base in HUMAN_NAMES:
        return "human"
    return "unknown"


# ----------------------------------------------------------------------------
# Regex patterns
# ----------------------------------------------------------------------------
RE_FINISH = re.compile(r"\bfinish\s*\(", re.I)
RE_BASH_FENCE = re.compile(r"```\s*(?:bash|shell|sh)\b", re.I)
RE_PYTHON_FENCE = re.compile(r"```\s*python\b", re.I)
RE_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)

RE_VERIFIER_PASS = re.compile(
    r"(VERDICT\s*[:=]\s*PASS|tests?\s+passed|all\s+tests\s+pass|verification\s+success|VERIFY\s*[:=]\s*PASS)",
    re.I,
)
RE_VERIFIER_FAIL = re.compile(
    r"(VERDICT\s*[:=]\s*FAIL|tests?\s+failed|assertion\s*error|verification\s+fail|VERIFY\s*[:=]\s*FAIL)",
    re.I,
)

RE_EXIT_CODE = re.compile(r"(?:exit[ _]?code|return[ _]?code)\s*[:=]\s*(-?\d+)", re.I)
RE_EXIT_CODE2 = re.compile(r"exited\s+with\s+code\s+(-?\d+)", re.I)
RE_ERROR_TOKENS = re.compile(r"\b(Error|Traceback|Exception|FAIL|fatal)\b")


# ----------------------------------------------------------------------------
# Heuristics
# ----------------------------------------------------------------------------
def detect_action_type(role: str, name_raw: str, content: str) -> str:
    if role == "user" and name_raw in HUMAN_NAMES:
        return "message"
    if role == "user" and name_raw in TERMINAL_NAMES:
        return "observation"
    if " (-> " in name_raw:
        return "delegate"
    if name_raw in VERIFIERS:
        return "judge"
    if RE_FINISH.search(content or ""):
        return "action_finish"
    if RE_BASH_FENCE.search(content or ""):
        return "action_bash"
    if RE_PYTHON_FENCE.search(content or ""):
        return "action_python"
    # 剥离 think 后还剩很少 → 单纯 thinking
    if "<think>" in (content or ""):
        stripped = RE_THINK_BLOCK.sub("", content or "").strip()
        if len(stripped) < 80:
            return "thinking"
    return "message"


def detect_verifier_signal(agent_norm: str, content: str) -> Optional[str]:
    if agent_norm not in {"verifier", "terminal"}:
        return None
    if not content:
        return None
    if RE_VERIFIER_FAIL.search(content):
        return "FAIL"
    if RE_VERIFIER_PASS.search(content):
        return "PASS"
    return None


def detect_exit_code(content: str) -> Optional[int]:
    if not content:
        return None
    m = RE_EXIT_CODE.search(content) or RE_EXIT_CODE2.search(content)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def detect_success(exit_code: Optional[int], content: str) -> Optional[bool]:
    if exit_code is not None:
        return exit_code == 0
    if content and RE_ERROR_TOKENS.search(content):
        return False
    return None


def compute_step_hash(step_id: int, name: str, content: str) -> str:
    head = (content or "")[: config.STEP_HASH_HEAD_CHARS]
    tail = (content or "")[-config.STEP_HASH_TAIL_CHARS:] if len(content or "") > config.STEP_HASH_HEAD_CHARS else ""
    key = f"{step_id}|{name}|{head}|{tail}"
    return hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()[:16]


# ----------------------------------------------------------------------------
# Public entry
# ----------------------------------------------------------------------------
def enrich_steps(history: List[Dict[str, Any]]) -> List[Step]:
    steps: List[Step] = []
    for raw in history:
        if not isinstance(raw, dict):
            continue
        sid = raw.get("step")
        role = raw.get("role") or "assistant"
        name_raw = raw.get("name") or "unknown"
        content = raw.get("content")
        if not isinstance(content, str):
            content = "" if content is None else str(content)
        if not isinstance(sid, int):
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                continue

        agent_norm = normalize_agent_name(name_raw)
        action_type = detect_action_type(role, name_raw, content)
        verifier_sig = detect_verifier_signal(agent_norm, content)
        exit_code = detect_exit_code(content)
        success = detect_success(exit_code, content)
        step_hash = compute_step_hash(sid, name_raw, content)

        steps.append(
            Step(
                step_id=sid,
                role=role,  # type: ignore[arg-type]
                agent_name_raw=name_raw,
                agent_normalized=agent_norm,  # type: ignore[arg-type]
                action_type=action_type,  # type: ignore[arg-type]
                content_full=content,
                content_head=content[: config.STEP_HEAD_CHARS],
                content_tail=content[-config.STEP_TAIL_CHARS:] if len(content) > config.STEP_HEAD_CHARS else "",
                content_len=len(content),
                step_hash=step_hash,
                success=success,
                exit_code=exit_code,
                verifier_signal=verifier_sig,  # type: ignore[arg-type]
            )
        )
    return steps
