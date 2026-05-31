"""跑批进度面板:扫 data(输入)与 llm_analysis(输出)每 bench 的文件数,打 done/total/%。

run.py 自身只知道当前这个 bench 的进度;本工具一次列出**全部 bench 的全局进度**,
适合多天大跑时在**另一个终端**随时查(或挂 --watch 自动刷新)。

用法(从 scripts/ 父目录,或直接 python 本文件):
  python -m MAS_trajectory_analysis.tools.progress
  python -m MAS_trajectory_analysis.tools.progress --watch 30      # 每 30s 刷新一次(Ctrl-C 退出)
  DATA_DIR=/path OUT_DIR=/path python -m MAS_trajectory_analysis.tools.progress   # 覆盖目录(同 run_all_benches.sh)

说明:
  - total = data/<bench>/*.json 输入数;done = llm_analysis/<bench>/*.json 已写出数(均排除 .debug.json)。
  - 输出文件是**原子写**,文件存在=该轨迹已跑完,所以数文件即进度(快,不解析内容)。
  - 注意:is_correct=True 的输入**不产出文件**(直接跳过),故个别 bench 的 done 可能**合理地**停在 total 之下。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

DEFAULT_DATA = "/volume/coder/users/yzli02/swwu/jincheng/fengboyu/CNIC/zhangyunfei/data"
DEFAULT_OUT = "/volume/coder/users/yzli02/swwu/jincheng/fengboyu/CNIC/zhangyunfei/llm_analysis"
DEFAULT_BENCHES = [
    "swe_bench_pro", "terminal_bench_2", "travelplanner", "vitabench", "webarena_verified",
]


def _count_json(d: Path) -> int:
    """目录下非 .debug.json 的 *.json 个数;目录不存在记 0。"""
    if not d.is_dir():
        return 0
    return sum(
        1 for p in d.iterdir()
        if p.is_file() and p.suffix == ".json" and not p.name.endswith(".debug.json")
    )


def _bar(frac: float, width: int = 24) -> str:
    n = max(0, min(width, int(round(frac * width))))
    return "█" * n + "░" * (width - n)


def render(data_dir: Path, out_dir: Path, benches) -> str:
    lines = [
        f"{'bench':<20} {'done':>6}/{'total':<6} {'%':>5}  进度",
        "-" * 72,
    ]
    tot_done = tot_all = 0
    for b in benches:
        total = _count_json(data_dir / b)
        done = _count_json(out_dir / b)
        tot_done += done
        tot_all += total
        frac = (done / total) if total else 0.0
        lines.append(f"{b:<20} {done:>6}/{total:<6} {frac*100:>4.0f}%  {_bar(frac)}")
    lines.append("-" * 72)
    frac = (tot_done / tot_all) if tot_all else 0.0
    lines.append(f"{'TOTAL':<20} {tot_done:>6}/{tot_all:<6} {frac*100:>4.0f}%  {_bar(frac)}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="跑批进度面板(每 bench done/total/%)")
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", DEFAULT_DATA),
                    help="输入根(其下 <bench>/*.json)")
    ap.add_argument("--out-dir", default=os.environ.get("OUT_DIR", DEFAULT_OUT),
                    help="输出根(其下 <bench>/*.json)")
    ap.add_argument("--benchmarks", nargs="*", default=DEFAULT_BENCHES, help="要统计的 bench")
    ap.add_argument("--watch", type=float, default=0.0, help="每 N 秒刷新(0=只打一次)")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)

    if args.watch and args.watch > 0:
        try:
            while True:
                sys.stdout.write("\x1b[2J\x1b[H")   # 清屏 + 光标归位
                print(f"[{time.strftime('%F %T')}]  data={data_dir}\n{'':14}out={out_dir}")
                print(render(data_dir, out_dir, args.benchmarks))
                print(f"\n(每 {args.watch:g}s 刷新,Ctrl-C 退出)")
                sys.stdout.flush()
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0
    else:
        print(f"[{time.strftime('%F %T')}]  data={data_dir}\n{'':14}out={out_dir}")
        print(render(data_dir, out_dir, args.benchmarks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
