"""混合分段:硬边界(语义切点)+ 软边界(长度目标)+ overlap。

返回 List[Segment]。Segment.steps 是本段独有步骤;Segment.overlap_steps 是上一段尾部
(用于 LLM 上下文,聚合时不重复计算)。
"""
from __future__ import annotations

from typing import List, Optional

from .. import config
from ..core.schema import Step, Segment


def _last_verifier_signal(prev_steps: List[Step]) -> Optional[str]:
    """从尾部找最近的 verifier_signal(非 None)。"""
    for s in reversed(prev_steps):
        if s.verifier_signal:
            return s.verifier_signal
    return None


def _last_agent_norm(prev_steps: List[Step]) -> Optional[str]:
    if not prev_steps:
        return None
    return prev_steps[-1].agent_normalized


def _segment_char_len(steps: List[Step]) -> int:
    return sum(s.content_len for s in steps)


def _emit_segment(
    seg_idx: int,
    cur: List[Step],
    prev_seg_tail: List[Step],
    reason: str,
) -> Segment:
    return Segment(
        segment_id=seg_idx,
        step_range=[cur[0].step_id, cur[-1].step_id],
        steps=cur,
        overlap_steps=prev_seg_tail,
        boundary_reason=reason,
        char_len=_segment_char_len(cur),
    )


def segment_trajectory(steps: List[Step]) -> List[Segment]:
    if not steps:
        return []

    segments: List[Segment] = []
    cur: List[Step] = []
    cur_chars = 0

    def cur_min_reached() -> bool:
        return len(cur) >= config.SEG_MIN_STEPS

    for idx, step in enumerate(steps):
        if not cur:
            cur.append(step)
            cur_chars += step.content_len
            continue

        last = cur[-1]
        reason: Optional[str] = None

        prev_norm = _last_agent_norm(cur)
        agent_changed = bool(prev_norm and step.agent_normalized != prev_norm)
        substantial = cur_chars >= config.SEG_MIN_CHARS_FOR_SOFT_BOUNDARY

        # 硬边界 1: 上一步是 finish + 当前段已积累一定信息
        # (finish 后通常是新阶段;但仅在当前段非纯 verifier-loop 时切)
        if last.action_type == "action_finish" and substantial and cur_min_reached():
            reason = "finish_signal"

        # 硬边界 2: verifier 信号变化(PASS↔FAIL 翻转)
        if reason is None:
            prev_sig = _last_verifier_signal(cur)
            if (step.verifier_signal and prev_sig
                    and step.verifier_signal != prev_sig
                    and substantial):
                reason = "verifier_signal_change"

        # 硬边界 3: agent 跳变 + 当前是 delegate/finish + 已达 min_steps + 已积累
        # (注意:不再让单纯 judge 触发,因 verification loop 中 judge 频繁出现)
        if reason is None:
            if (
                agent_changed
                and step.action_type in {"delegate", "action_finish"}
                and cur_min_reached()
                and substantial
            ):
                reason = "agent_shift"

        # 硬边界 4: 加入当前步会超字符上限
        if reason is None and cur_chars + step.content_len > config.SEG_MAX_CHARS:
            reason = "max_length"

        # 硬边界 5: 步数上限
        if reason is None and len(cur) >= config.SEG_MAX_STEPS:
            reason = "max_steps"

        # 软边界: 到目标长度且 agent 跳变
        if reason is None and cur_chars >= config.SEG_TARGET_CHARS and agent_changed:
            reason = "soft_agent_shift_at_target"

        if reason is not None:
            prev_tail = cur[-config.SEG_OVERLAP_STEPS:] if cur else []
            segments.append(_emit_segment(len(segments), cur, _prev_overlap(segments), reason))
            cur = list(prev_tail)        # 新段从 overlap tail 开始(只读上下文)
            cur_chars = _segment_char_len(cur)
            # 然后追加当前步;但 overlap 步会被记到 overlap_steps,不计入当前段 steps
            cur = []                      # 实际当前段从 step 开始
            cur_chars = 0
            cur.append(step)
            cur_chars += step.content_len
        else:
            cur.append(step)
            cur_chars += step.content_len

    if cur:
        segments.append(_emit_segment(len(segments), cur, _prev_overlap(segments), "end_of_trace"))

    # 后处理:把每一段的 overlap_steps 填上
    for i, seg in enumerate(segments):
        if i == 0:
            seg.overlap_steps = []
        else:
            prev = segments[i - 1]
            seg.overlap_steps = prev.steps[-config.SEG_OVERLAP_STEPS:]

    return segments


def _prev_overlap(segments: List[Segment]) -> List[Step]:
    if not segments:
        return []
    return segments[-1].steps[-config.SEG_OVERLAP_STEPS:]


def split_in_half(seg: Segment) -> List[Segment]:
    """运行时兜底:某段意外仍超 token 时把其二分。"""
    if len(seg.steps) <= 1:
        return [seg]
    mid = len(seg.steps) // 2
    left = Segment(
        segment_id=seg.segment_id,
        step_range=[seg.steps[0].step_id, seg.steps[mid - 1].step_id],
        steps=seg.steps[:mid],
        overlap_steps=seg.overlap_steps,
        boundary_reason="split_in_half_left",
        char_len=_segment_char_len(seg.steps[:mid]),
    )
    right_overlap = seg.steps[max(0, mid - config.SEG_OVERLAP_STEPS): mid]
    right = Segment(
        segment_id=seg.segment_id,                  # 调用方需要重新编号
        step_range=[seg.steps[mid].step_id, seg.steps[-1].step_id],
        steps=seg.steps[mid:],
        overlap_steps=right_overlap,
        boundary_reason="split_in_half_right",
        char_len=_segment_char_len(seg.steps[mid:]),
    )
    return [left, right]
