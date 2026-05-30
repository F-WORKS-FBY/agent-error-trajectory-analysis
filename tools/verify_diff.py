"""验证 v2 输出文件相对原文件:除了 4 个 v2 字段外其余顶层字段字节级相同。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import config

V2_FIELDS = {
    "llm_mistake_agent",
    "llm_mistake_step",
    "llm_mistake_reason",
    "llm_analysis_summary",
}


def check_one(src_path: Path, out_path: Path) -> tuple[bool, list[str]]:
    src = json.loads(src_path.read_text(encoding="utf-8"))
    out = json.loads(out_path.read_text(encoding="utf-8"))

    errs: list[str] = []

    new_keys = set(out.keys()) - set(src.keys())
    expected_new = V2_FIELDS - set(src.keys())
    unexpected_new = new_keys - V2_FIELDS
    missing_v2 = V2_FIELDS - set(out.keys())
    if unexpected_new:
        errs.append(f"unexpected new top-level keys: {sorted(unexpected_new)}")
    if missing_v2:
        errs.append(f"missing v2 keys: {sorted(missing_v2)}")

    dropped = set(src.keys()) - set(out.keys())
    if dropped:
        errs.append(f"dropped original keys: {sorted(dropped)}")

    for k in src.keys():
        if k in V2_FIELDS:
            continue
        if src[k] != out.get(k):
            errs.append(f"top-level field changed: {k}")

    return (len(errs) == 0), errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", "-b", required=True)
    ap.add_argument("--limit", "-n", type=int, default=0)
    args = ap.parse_args()

    src_dir = config.WHO_WHEN_DIR / args.benchmark
    out_dir = config.OUT_ROOT / args.benchmark
    if not out_dir.exists():
        print(f"out dir missing: {out_dir}")
        return 2

    out_files = sorted(p for p in out_dir.glob("*.json") if not p.name.endswith(".debug.json"))
    if args.limit > 0:
        out_files = out_files[: args.limit]

    n_ok = n_bad = 0
    for op in out_files:
        sp = src_dir / op.name
        if not sp.exists():
            print(f"MISSING SRC: {op.name}")
            n_bad += 1
            continue
        ok, errs = check_one(sp, op)
        if ok:
            n_ok += 1
        else:
            n_bad += 1
            print(f"FAIL {op.name}:")
            for e in errs:
                print(f"  - {e}")
    print(f"\nDONE. ok={n_ok} bad={n_bad}")
    return 0 if n_bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
