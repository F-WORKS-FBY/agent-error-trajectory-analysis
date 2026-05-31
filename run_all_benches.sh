#!/usr/bin/env bash
# =============================================================================
# 一键跑 CNIC/zhangyunfei/data 下的 5 个 bench 根因分析(分段→局部摘要→阶段聚合→根因判定)。
#
# 用法:
#   WORKERS=10 ./run_all_benches.sh               # 10 并发,跑全部 5 个 bench(默认 workers=8)
#   OVERWRITE=1 WORKERS=10 ./run_all_benches.sh   # 用当前 prompt 重算并**覆盖**已有输出(换了 prompt 要全量重标时用)
#   ./run_all_benches.sh swe_bench_pro vitabench  # 只跑指定 bench(空格分隔)
#   LIMIT=3 ./run_all_benches.sh                  # 每个 bench 只跑前 3 个(冒烟)
#   DATA_DIR=/path OUT_DIR=/path ./run_all_benches.sh   # 覆盖输入/输出目录
#
# 进度:跑起来后,在**另一个终端**随时查全局进度(每 bench done/total/%):
#   python -m MAS_trajectory_analysis.tools.progress          # 打一次
#   python -m MAS_trajectory_analysis.tools.progress --watch 30   # 每 30s 刷新
#   (本脚本运行中每行也会打 [bench done/total] 的实时计数。)
#
# 断点重跑:中途断了(Ctrl-C / 掉线 / 报错),直接**不带 OVERWRITE**再跑一次本脚本即可断点续跑
#           (已完成文件自动跳过 is_complete_v2,不重算)。⚠ 续跑时**别再带 OVERWRITE=1**,否则会把
#           已跑完的也重算一遍。换 prompt 重标的正确姿势:首跑带 OVERWRITE=1,中断后续跑去掉它。
#           源文件 data/ 永不被覆盖(只读)。
#
# 安全:本脚本**不含**任何 key。key 从同目录的 .env(已 gitignore,不会进仓库)
#       或环境变量 LLM_API_KEY 读取。绝不要把 key 写进本脚本。
# =============================================================================
set -uo pipefail

# ---- 路径自解析(不管从哪个目录调用都正确)----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../scripts/MAS_trajectory_analysis
SCRIPTS_DIR="$(dirname "$SCRIPT_DIR")"                        # .../scripts (python -m 必须从这里跑)

# ---- 载入同目录 .env(若存在)----
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a; source "$SCRIPT_DIR/.env"; set +a
  echo "[info] 已载入 $SCRIPT_DIR/.env"
fi

# ---- 校验 API key(缺失即退出并给出指引)----
if [[ -z "${LLM_API_KEY:-}" && -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "[ERROR] LLM_API_KEY 未设置。" >&2
  echo "  方式一(推荐):cp .env.example .env 并把 key 填进去(.env 已 gitignore,不会进仓库)" >&2
  echo "  方式二:export LLM_API_KEY=sk-...  再跑本脚本" >&2
  exit 1
fi

# ---- 可配置项(均可用环境变量覆盖)----
DATA_DIR="${DATA_DIR:-/volume/coder/users/yzli02/swwu/jincheng/fengboyu/CNIC/zhangyunfei/data}"
OUT_DIR="${OUT_DIR:-/volume/coder/users/yzli02/swwu/jincheng/fengboyu/CNIC/zhangyunfei/llm_analysis}"
WORKERS="${WORKERS:-32}"   # 默认 32 并发(smoke 实测 10 并发零限流,deepseek 余量足);可用 WORKERS=N 覆盖

# bench 列表:命令行传了就用命令行的,否则默认 5 个
if [[ $# -gt 0 ]]; then
  BENCHES=("$@")
else
  BENCHES=(swe_bench_pro terminal_bench_2 travelplanner vitabench webarena_verified)
fi

# 可选开关:OVERWRITE / DEBUG_SIDECAR / LIMIT → 拼成额外 flag(透传给 run.py)
EXTRA_FLAGS=()
[[ "${OVERWRITE:-0}" == "1" ]]     && EXTRA_FLAGS+=(--overwrite)
[[ "${DEBUG_SIDECAR:-0}" == "1" ]] && EXTRA_FLAGS+=(--debug-sidecar)
[[ -n "${LIMIT:-}" ]]              && EXTRA_FLAGS+=(--limit "$LIMIT")

echo "============================================================"
echo " 输入:  $DATA_DIR"
echo " 输出:  $OUT_DIR"
echo " 并发:  workers=$WORKERS"
echo " bench: ${BENCHES[*]}"
echo " 模型:  ${LLM_MODEL:-deepseek-v4-pro} @ ${LLM_BASE_URL:-https://api.deepseek.com}"
echo " 额外:  ${EXTRA_FLAGS[*]:-(无;默认断点续跑、不覆盖)}"
echo " 进度:  python -m MAS_trajectory_analysis.tools.progress [--watch 30]"
echo " 日志:  $SCRIPT_DIR/logs/(每个 bench 一个带时间戳的 .log)"
echo "============================================================"

cd "$SCRIPTS_DIR"

rc_total=0
for b in "${BENCHES[@]}"; do
  echo ""
  echo "==== $(date '+%F %T') START $b ===="
  python -m MAS_trajectory_analysis.run \
    --benchmark "$b" \
    --input-dir "$DATA_DIR" \
    --output-dir "$OUT_DIR" \
    --workers "$WORKERS" \
    "${EXTRA_FLAGS[@]}"
  rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "[WARN] bench '$b' 退出码=$rc(本 bench 可能未跑完);继续下一个。重跑本脚本会断点续跑。" >&2
    rc_total=$rc
  fi
  echo "==== $(date '+%F %T') END   $b (rc=$rc) ===="
done

echo ""
echo "==== $(date '+%F %T') ALL DONE ===="
echo "结果在: $OUT_DIR/<bench>/<file>.json"
echo "(若上面出现过 [WARN],重跑本脚本即可断点续跑未完成的文件。)"
exit $rc_total
