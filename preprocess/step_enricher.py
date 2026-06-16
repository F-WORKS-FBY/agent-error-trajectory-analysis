"""把原始 history dict 列表转成 Step。

负责:
- 字段映射(经 DatasetProfile:step_id / role / agent 名 / content 在哪)
- normalize_agent_name(经 profile 的角色集或 passthrough 归一)
- 委派解析(经 profile 的 DelegationSpec → Step.delegate_target)
- action_type 决策树
- verifier_signal / exit_code / success 启发式
- step_hash 指纹

通用化要点:
- 缺 step_id 字段时**按 history 下标枚举**(改造前会整步丢弃 → 空轨迹)。
- 角色集 / 委派记号全部来自 profile;默认 profile 复刻改造前行为(旧 5 bench 字节级不变)。
"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, Any, List, Optional

from .. import config
from ..core.schema import Step
from .profile import (
    DatasetProfile, DEFAULT_PROFILE,
    DEFAULT_PLANNERS, DEFAULT_EXECUTORS, DEFAULT_VERIFIERS,
    DEFAULT_TERMINALS, DEFAULT_HUMANS,
)


# ----------------------------------------------------------------------------
# 向后兼容别名(改造前这些是本模块的模块级常量;现在权威定义在 profile.py)
# ----------------------------------------------------------------------------
PLANNERS = DEFAULT_PLANNERS
EXECUTORS = DEFAULT_EXECUTORS
VERIFIERS = DEFAULT_VERIFIERS
TERMINAL_NAMES = DEFAULT_TERMINALS
HUMAN_NAMES = DEFAULT_HUMANS


def normalize_agent_name(name: str, profile: DatasetProfile = DEFAULT_PROFILE) -> str:
    """按 profile 角色集(或 passthrough)把 agent 名归一。"""
    return profile.normalize_role(name)


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
def detect_action_type(
    role: str,
    name_raw: str,
    content: str,
    delegate_target: Optional[str],
    profile: DatasetProfile = DEFAULT_PROFILE,
) -> str:
    base = profile.delegation.strip_sender(name_raw)
    if role == "user" and base in profile.humans:
        return "message"
    if role == "user" and base in profile.terminals:
        return "observation"
    if delegate_target is not None:
        return "delegate"
    if base in profile.verifiers:
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
def enrich_steps(
    history: List[Dict[str, Any]],
    profile: DatasetProfile = DEFAULT_PROFILE,
) -> List[Step]:
    steps: List[Step] = []
    for idx, raw in enumerate(history):
        if not isinstance(raw, dict):
            continue

        # step_id:profile 指定字段且为整数则用;否则按 history 下标枚举(不再丢步)。
        sid: Optional[int] = None
        if profile.step_id_field:
            v = raw.get(profile.step_id_field)
            if isinstance(v, int):
                sid = v
            else:
                try:
                    sid = int(v)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    sid = idx
        else:
            sid = idx

        name_raw = profile.extract_agent_name(raw)
        role = profile.extract_message_role(raw, name_raw)
        content = profile.extract_content(raw)

        delegate_target = profile.resolve_delegate_target(raw, name_raw, content)
        agent_norm = profile.normalize_role(name_raw)
        action_type = detect_action_type(role, name_raw, content, delegate_target, profile)
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
                delegate_target=delegate_target,
            )
        )
    return steps
