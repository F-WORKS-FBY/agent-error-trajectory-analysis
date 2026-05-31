#!/usr/bin/env bash
# =============================================================================
# 试水脚本:只跑 swe_bench_pro 一个 bench 的根因分析(分段→局部摘要→阶段聚合→根因判定)。
# 其它 4 个 bench 不碰。用来在大跑前验证当前 config(per-stage thinking)的标注质量。
#
# 用法:
#   ./run_swe.sh                       # workers=5,跑 swe_bench_pro 全部 157 个失败轨迹
#   WORKERS=8 ./run_swe.sh             # 调大并发(API 额度够时最有效的提速杠杆)
#   DEBUG_SIDECAR=1 ./run_swe.sh       # 额外写 <name>.debug.json(含 Stage4 思维链),试水强烈建议开
#   OVERWRITE=1 ./run_swe.sh           # 覆盖已存在输出(默认断点续跑、不覆盖)
#   LIMIT=3 ./run_swe.sh               # 只跑前 3 个文件(快速冒烟)
#   DATA_DIR=/path OUT_DIR=/path ./run_swe.sh   # 覆盖输入/输出目录
#
# 断点重跑:中途断了(Ctrl-C / 掉线 / 报错),直接再跑一次本脚本即可。
#           已完成的文件自动跳过(is_complete_v2),不重算、不覆盖 data/ 源文件。
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
WORKERS="${WORKERS:-5}"
BENCH="swe_bench_pro"

# 可选开关:DEBUG_SIDECAR / OVERWRITE / LIMIT → 拼成额外 flag
EXTRA_FLAGS=()
[[ "${DEBUG_SIDECAR:-0}" == "1" ]] && EXTRA_FLAGS+=(--debug-sidecar)
[[ "${OVERWRITE:-0}" == "1" ]]     && EXTRA_FLAGS+=(--overwrite)
[[ -n "${LIMIT:-}" ]]              && EXTRA_FLAGS+=(--limit "$LIMIT")

echo "============================================================"
echo " bench: $BENCH(只跑这一个)"
echo " 输入:  $DATA_DIR/$BENCH/*.json"
echo " 输出:  $OUT_DIR/$BENCH/*.json"
echo " 并发:  workers=$WORKERS"
echo " 模型:  ${LLM_MODEL:-deepseek-v4-pro} @ ${LLM_BASE_URL:-https://api.deepseek.com}"
echo " thinking: local/phase/root 由 config.py 决定(当前 3 阶段均 thinking=high)"
echo " 额外:  ${EXTRA_FLAGS[*]:-(无)}"
echo " 日志:  $SCRIPT_DIR/logs/(带时间戳的 .log)"
echo "============================================================"

cd "$SCRIPTS_DIR"

echo ""
echo "==== $(date '+%F %T') START $BENCH ===="
python -m MAS_trajectory_analysis.run \
  --benchmark "$BENCH" \
  --input-dir "$DATA_DIR" \
  --output-dir "$OUT_DIR" \
  --workers "$WORKERS" \
  "${EXTRA_FLAGS[@]}"
rc=$?
echo "==== $(date '+%F %T') END   $BENCH (rc=$rc) ===="

if [[ $rc -ne 0 ]]; then
  echo "[WARN] 退出码=$rc,可能未跑完;重跑本脚本会断点续跑未完成的文件。" >&2
fi
echo "结果在: $OUT_DIR/$BENCH/<file>.json"
exit $rc
