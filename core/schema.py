"""v2 数据结构定义。所有 LLM 输入/输出、内部传递对象在此处统一。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any, Literal


# ----------------------------------------------------------------------------
# Step (启发式抽取后)
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Step:
    step_id: int
    role: Literal["user", "assistant"]
    agent_name_raw: str
    agent_normalized: Literal[
        "planner", "executor", "verifier", "terminal", "human", "unknown"
    ]
    action_type: Literal[
        "thinking", "action_bash", "action_python", "action_finish",
        "delegate", "observation", "judge", "message",
    ]
    content_full: str
    content_head: str
    content_tail: str
    content_len: int
    step_hash: str
    success: Optional[bool] = None
    exit_code: Optional[int] = None
    verifier_signal: Optional[Literal["PASS", "FAIL"]] = None

    def to_lite_dict(self) -> Dict[str, Any]:
        """用于摘要 prompt 注入的瘦字段表示。"""
        return {
            "step_id": self.step_id,
            "agent": self.agent_name_raw,
            "agent_role": self.agent_normalized,
            "action_type": self.action_type,
            "exit_code": self.exit_code,
            "verifier_signal": self.verifier_signal,
            "content_head": self.content_head,
            "content_tail": self.content_tail,
            "content_len": self.content_len,
            "step_hash": self.step_hash,
        }


# ----------------------------------------------------------------------------
# Segment
# ----------------------------------------------------------------------------
@dataclass
class Segment:
    segment_id: int
    step_range: List[int]                     # [first_step_id, last_step_id]
    steps: List[Step]
    overlap_steps: List[Step]                 # 来自上一段尾部
    boundary_reason: str                      # finish_signal/verifier_signal_change/agent_shift/max_length/max_steps/soft_agent_shift_at_target/end_of_trace
    char_len: int

    @property
    def agent_set(self) -> List[str]:
        seen = []
        for s in self.steps:
            if s.agent_name_raw not in seen:
                seen.append(s.agent_name_raw)
        return seen

    @property
    def verifier_signal_seq(self) -> List[str]:
        return [s.verifier_signal for s in self.steps if s.verifier_signal]

    @property
    def step_ids(self) -> List[int]:
        return [s.step_id for s in self.steps]

    def to_meta_dict(self) -> Dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "step_range": self.step_range,
            "boundary_reason": self.boundary_reason,
            "agent_set": self.agent_set,
            "verifier_signal_seq": self.verifier_signal_seq,
            "char_len": self.char_len,
            "step_hashes": {s.step_id: s.step_hash for s in self.steps},
        }


# ----------------------------------------------------------------------------
# LocalSummary (LLM 局部摘要输出)
# ----------------------------------------------------------------------------
@dataclass
class LocalSummary:
    segment_id: int
    step_range: List[int]
    segment_goal: str = ""
    key_events: List[Dict[str, Any]] = field(default_factory=list)
    candidate_failures: List[Dict[str, Any]] = field(default_factory=list)
    verifier_findings: List[Dict[str, Any]] = field(default_factory=list)
    uncertainties: List[str] = field(default_factory=list)
    needs_human_review: bool = False
    raw_text: str = ""                          # LLM 原始输出,审计用

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("raw_text", None)
        return d


# ----------------------------------------------------------------------------
# PhaseSummary (LLM 阶段聚合输出)
# ----------------------------------------------------------------------------
@dataclass
class PhaseSummary:
    phases: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    raw_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"phases": self.phases, "conflicts": self.conflicts}


# ----------------------------------------------------------------------------
# RootCauseAnnotation (LLM 根因判定输出)
# ----------------------------------------------------------------------------
@dataclass
class RootCauseAnnotation:
    agent: str = ""
    step: str = ""                              # "186" 或 "system_evaluation"
    reason: str = ""
    evidence_step_ids: List[int] = field(default_factory=list)
    abstain: bool = False
    needs_human_review: bool = False
    primary_category: str = ""                  # 单选主因,∈ CATEGORY_CODES
    contributing_factors: List[str] = field(default_factory=list)  # 可选次因,⊆ CATEGORY_CODES
    detailed_analysis: Dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("raw_text", None)
        return d
