"""v2 CLI 主入口。

  python -m MAS_trajectory_analysis.run --benchmark swe_bench_pro --limit 5 --workers 3

或从 scripts/ 目录:

  cd scripts && python -m MAS_trajectory_analysis.run --benchmark swe_bench_pro
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Any, List, Optional

# 允许直接 `python run.py` 调用
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "MAS_trajectory_analysis"

from . import config
from .preprocess.loader import list_input_files, load_trajectory, build_task_brief
from .preprocess.segmenter import segment_trajectory, split_in_half
from .core.llm_client import DeepSeekClient
from .analyze.local_summarizer import summarize_segment
from .analyze.global_reducer import (
    aggregate_phases, diagnose_root_cause, build_verifier_signal_summary,
    compress_step_ids,
)
from .analyze.validator import (
    validate_local_summary, coerce_local_summary,
    validate_phase_summary, coerce_phase_summary,
    validate_root_cause, coerce_root_cause,
)
from .output.io_writer import (
    inject_v2_fields, write_v2_result, write_debug_sidecar, out_path_for,
    is_complete_v2, _step_to_int,
)
from .output.presenter import build_lean_summary
from .core.schema import LocalSummary, PhaseSummary, RootCauseAnnotation, Segment


LOG = logging.getLogger("MAS_trajectory_analysis")

_shutdown_event = threading.Event()


# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------
def setup_logging(verbose: bool, log_file: Optional[Path] = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)


# ----------------------------------------------------------------------------
# Summary object builder
# ----------------------------------------------------------------------------
def _segment_meta(seg: Segment) -> Dict[str, Any]:
    return {
        "segment_id": seg.segment_id,
        "step_range": seg.step_range,
        "boundary_reason": seg.boundary_reason,
        "agent_set": seg.agent_set,
        "verifier_signal_seq": seg.verifier_signal_seq,
        "char_len": seg.char_len,
        "n_steps": len(seg.steps),
    }


def build_summary_object(
    n_steps: int,
    segments: List[Segment],
    locals_: List[LocalSummary],
    phases: PhaseSummary,
    ann: RootCauseAnnotation,
    task_brief: str,
) -> Dict[str, Any]:
    return {
        "schema_version": config.SCHEMA_VERSION,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "model": config.LLM_MODEL,
        "prompt_versions": dict(config.PROMPT_VERSIONS),
        "n_steps": n_steps,
        "n_segments": len(segments),
        "n_phases": len(phases.phases or []),
        "task_brief": task_brief[:4000],
        "segments": [_segment_meta(s) for s in segments],
        "local_summaries": [ls.to_dict() for ls in locals_],
        "phases": phases.to_dict(),
        "root_cause": {
            "agent": ann.agent,
            "step": ann.step,
            "reason": ann.reason,
            "evidence_step_ids": ann.evidence_step_ids,
            "abstain": ann.abstain,
            "needs_human_review": ann.needs_human_review,
            "primary_category": ann.primary_category,
            "contributing_factors": ann.contributing_factors,
            "detailed_analysis": ann.detailed_analysis,
        },
    }


# ----------------------------------------------------------------------------
# Pipeline
# ----------------------------------------------------------------------------
def process_one(
    src_path: Path,
    bench: str,
    client: DeepSeekClient,
    out_root: Path,
    overwrite: bool,
    dry_run: bool,
    debug_sidecar: bool = False,
) -> str:
    if _shutdown_event.is_set():
        return "cancelled"

    out_path = out_path_for(bench, src_path.name, out_root=out_root)
    if not overwrite and is_complete_v2(out_path):
        return "skipped_existing"

    data, steps = load_trajectory(src_path)
    if data.get("is_correct", False):
        return "skipped_success"
    if not steps:
        return "skipped_empty"

    step_id_set = {s.step_id for s in steps}
    valid_agents_raw = sorted({s.agent_name_raw for s in steps})
    task_brief = build_task_brief(data, src_path.name)

    segments = segment_trajectory(steps)
    # 保险:若有段超 MAX,二分
    safe_segments: List[Segment] = []
    for seg in segments:
        if seg.char_len > config.SEG_MAX_CHARS and len(seg.steps) > 1:
            safe_segments.extend(split_in_half(seg))
        else:
            safe_segments.append(seg)
    # 重编号
    for i, seg in enumerate(safe_segments):
        seg.segment_id = i
    segments = safe_segments

    if dry_run:
        max_chars = max((s.char_len for s in segments), default=0)
        return (
            f"dry: file={src_path.name} steps={len(steps)} n_segs={len(segments)} "
            f"max_seg_chars={max_chars} max_seg_steps={max((len(s.steps) for s in segments), default=0)}"
        )

    # Stage 2: local summaries
    locals_: List[LocalSummary] = []
    for seg in segments:
        if _shutdown_event.is_set():
            return "cancelled"
        seg_ids = {s.step_id for s in seg.steps}
        ls = summarize_segment(client, seg, task_brief)
        ok, errs = validate_local_summary(ls, seg_ids)
        if not ok:
            LOG.warning("seg %d local validate fail: %s", seg.segment_id, "; ".join(errs[:3]))
            ls = summarize_segment(client, seg, task_brief, previous_errors=errs)
            ok2, errs2 = validate_local_summary(ls, seg_ids)
            if not ok2:
                ls = coerce_local_summary(ls, seg_ids)
                ls.needs_human_review = True
        locals_.append(ls)

    if _shutdown_event.is_set():
        return "cancelled"

    # Stage 3: phase aggregation
    phases = aggregate_phases(
        client, locals_, task_brief,
        global_step_ids=step_id_set,
        verifier_signal_summary=build_verifier_signal_summary(steps),
    )
    ok, errs = validate_phase_summary(phases, step_id_set)
    if not ok:
        LOG.warning("phase validate fail: %s", "; ".join(errs[:3]))
        phases = aggregate_phases(
            client, locals_, task_brief,
            global_step_ids=step_id_set,
            verifier_signal_summary=build_verifier_signal_summary(steps),
            previous_errors=errs,
        )
        ok2, errs2 = validate_phase_summary(phases, step_id_set)
        if not ok2:
            phases = coerce_phase_summary(phases, step_id_set)

    if _shutdown_event.is_set():
        return "cancelled"

    # Stage 4: root cause
    ann = diagnose_root_cause(
        client, phases, locals_, steps, data, task_brief,
        global_step_ids=step_id_set,
        valid_agents_raw=valid_agents_raw,
    )
    ok, errs = validate_root_cause(ann, step_id_set, set(valid_agents_raw), all_steps=steps)
    if not ok:
        LOG.warning("root_cause validate fail: %s", "; ".join(errs[:3]))
        ann = diagnose_root_cause(
            client, phases, locals_, steps, data, task_brief,
            global_step_ids=step_id_set,
            valid_agents_raw=valid_agents_raw,
            previous_errors=errs,
        )
        ok2, errs2 = validate_root_cause(ann, step_id_set, set(valid_agents_raw), all_steps=steps)
        if not ok2:
            LOG.warning("root_cause still invalid after retry: %s", "; ".join(errs2[:3]))
            ann = coerce_root_cause(ann, step_id_set, set(valid_agents_raw), all_steps=steps)
            ann.needs_human_review = True
            # 关键字段实在缺时:abstain 兜底
            if not ann.agent or not ann.step or not ann.primary_category:
                ann.abstain = True

    # 内部完整 summary(供 debug sidecar / 审计),不直接写进主输出
    full_summary = build_summary_object(
        n_steps=len(steps), segments=segments,
        locals_=locals_, phases=phases, ann=ann, task_brief=task_brief,
    )
    # 派生精简、纯展示型的 llm_analysis_summary(平台只读这一块)
    lean_summary = build_lean_summary(full_summary, ann, data, steps, src_path.name)

    injected = inject_v2_fields(
        original=data,
        llm_mistake_agent=ann.agent or "",
        llm_mistake_step=_step_to_int(ann.step) if ann.step else config.SYSTEM_EVAL_STEP_SENTINEL,
        llm_mistake_reason=ann.reason or "",          # reason 现在就是唯一的完整解释
        llm_analysis_summary=lean_summary,
    )
    write_v2_result(out_root, bench, src_path.name, injected)
    if debug_sidecar:
        write_debug_sidecar(out_root, bench, src_path.name,
                            {"full_summary": full_summary, "lean_summary": lean_summary})
    return "ok" if not ann.needs_human_review else "ok_needs_review"


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="多 Agent 失败轨迹根因分析(分段→局部摘要→阶段聚合→根因判定)")
    ap.add_argument("--input-dir", "-i", default=None,
                    help="输入目录。给出后:有 --benchmark 则读 <input-dir>/<benchmark>/*.json;"
                         "无 --benchmark 则读 <input-dir>/*.json(扁平)。默认用内置 Who&When_style 路径。")
    ap.add_argument("--output-dir", "-o", default=None, help="输出目录(默认内置 MAS_trajectory_analysis 路径)")
    ap.add_argument("--benchmark", "-b", default=None,
                    help="bench 子目录名(经典布局)。扁平目录可省略。")
    ap.add_argument("--limit", "-n", type=int, default=0, help="只处理前 N 个文件(0=全部)")
    ap.add_argument("--workers", "-w", type=int, default=1, help="并发数")
    ap.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出")
    ap.add_argument("--dry-run", action="store_true", help="只跑分段,不调 LLM")
    ap.add_argument("--debug-sidecar", action="store_true",
                    help="额外把完整中间结果写到 <name>.debug.json(审计用)")
    ap.add_argument("--verbose", "-v", action="store_true", help="DEBUG 日志")
    ap.add_argument("--file", help="只处理指定文件名")
    ap.add_argument("--out-root", default=None, help="(兼容旧参数)等价于 --output-dir")
    ap.add_argument("--model", default=None, help="覆盖 LLM 模型(默认 env LLM_MODEL / DEEPSEEK_MODEL)")
    ap.add_argument("--base-url", default=None, help="覆盖 LLM base_url(默认 env LLM_BASE_URL / DEEPSEEK_BASE_URL)")
    return ap.parse_args(argv)


def _install_signal_handler() -> None:
    def _handler(signum, frame):
        LOG.warning("received signal %d, requesting graceful shutdown", signum)
        _shutdown_event.set()
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    label = args.benchmark or (Path(args.input_dir).name if args.input_dir else "flat")
    log_file = config.LOGS_DIR / f"v2_{label}_{dt.datetime.now():%Y%m%d_%H%M%S}.log"
    setup_logging(args.verbose, log_file=log_file)

    _install_signal_handler()

    input_dir = Path(args.input_dir) if args.input_dir else config.WHO_WHEN_DIR
    out_root = Path(args.output_dir or args.out_root or config.OUT_ROOT)
    out_root.mkdir(parents=True, exist_ok=True)

    # 经典布局: <input-dir>/<benchmark>/*.json,bench 标签=benchmark;
    # 扁平布局(无 benchmark): <input-dir>/*.json,bench 标签=""(输出也扁平)。
    bench_label = args.benchmark or ""
    try:
        files = list_input_files(input_dir, args.benchmark)
    except FileNotFoundError as e:
        LOG.error("%s", e)
        return 2
    if args.file:
        files = [p for p in files if p.name == args.file]
        if not files:
            LOG.error("file not found: %s", args.file)
            return 2
    if args.limit > 0:
        files = files[: args.limit]

    LOG.info("input_dir=%s benchmark=%s n_files=%d workers=%d dry_run=%s overwrite=%s debug_sidecar=%s out=%s",
             input_dir, args.benchmark, len(files), args.workers, args.dry_run,
             args.overwrite, args.debug_sidecar, out_root)

    client = (
        DeepSeekClient(
            model=args.model or config.LLM_MODEL,
            base_url=args.base_url or config.LLM_BASE_URL,
        )
        if not args.dry_run else None  # type: ignore[assignment]
    )
    if client is not None:
        LOG.info("LLM endpoint: model=%s base_url=%s", args.model or config.LLM_MODEL,
                 args.base_url or config.LLM_BASE_URL)

    def _run(p: Path) -> str:
        return process_one(p, bench_label, client, out_root, args.overwrite,
                           args.dry_run, debug_sidecar=args.debug_sidecar)

    stats: Dict[str, int] = {}
    if args.workers <= 1:
        for p in files:
            try:
                status = _run(p)
            except Exception as e:
                LOG.exception("file %s failed: %s", p.name, e)
                status = f"error:{type(e).__name__}"
            stats[status] = stats.get(status, 0) + 1
            LOG.info("[%s] %s", status, p.name)
            if _shutdown_event.is_set():
                LOG.warning("shutdown requested, stopping after current file")
                break
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_run, p): p for p in files}
            for fu in as_completed(futs):
                p = futs[fu]
                try:
                    status = fu.result()
                except Exception as e:
                    LOG.exception("file %s failed: %s", p.name, e)
                    status = f"error:{type(e).__name__}"
                stats[status] = stats.get(status, 0) + 1
                LOG.info("[%s] %s", status, p.name)
                if _shutdown_event.is_set():
                    break

    LOG.info("DONE. stats=%s", json.dumps(stats, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
