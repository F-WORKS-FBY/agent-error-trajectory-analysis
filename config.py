"""v2 全局配置。所有常量集中此处,便于 CLI 覆盖或后续 v3 扩展。"""
from __future__ import annotations

import os
from pathlib import Path

# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------
# scripts/MAS_trajectory_analysis/config.py -> scripts/ -> MAS_trajectory_annotate/
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent              # .../MAS_trajectory_annotate
WHO_WHEN_DIR = REPO_ROOT / "Who&When_style"
OUT_ROOT = WHO_WHEN_DIR / "MAS_trajectory_analysis"

PROMPTS_DIR = SCRIPT_DIR / "prompts"
LEGACY_MAP_PATH = SCRIPT_DIR / "data" / "categories_legacy_map.json"
LOGS_DIR = SCRIPT_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# 5 bench 通用,但目前只有 swe_bench_pro 和 terminal_bench_2 落地
SUPPORTED_BENCHMARKS = [
    "swe_bench_pro",
    "terminal_bench_2",
    "travelplanner",
    "vitabench",
    "webarena_verified",
]

# ----------------------------------------------------------------------------
# LLM API (deepseek-v4-pro)
# ----------------------------------------------------------------------------
# provider 中性命名 LLM_* 优先,保留 DEEPSEEK_* 作向后兼容别名。适配任意 OpenAI 兼容服务。
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
LLM_MODEL = os.environ.get("LLM_MODEL") or os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-pro"

LLM_TIMEOUT_SECONDS = 600
LLM_MAX_RETRIES = 3
LLM_RETRY_BACKOFF_BASE = 4.0
# 注意:deepseek-v4-pro thinking 模式默认开启,thinking 下 temperature/top_p/惩罚项**全部失效**。
# 下面两个温度仅作非思考回退保留,实际不再以温度声称确定性(见 LLM_THINKING_ENABLED)。
LLM_TEMPERATURE_DEFAULT = 0.1            # (thinking 下失效) 摘要 / 阶段聚合
LLM_TEMPERATURE_ROOT = 0.0              # (thinking 下失效) 根因判定
LLM_MAX_TOKENS_LOCAL = 10000
LLM_MAX_TOKENS_PHASE = 20000
LLM_MAX_TOKENS_ROOT = 32000              # root:thinking 链 + 最终 JSON 共用输出额度,设到 API 上限附近,
                                         # 第一次就写得完 → 不触发 length 重试(重试会重发整个输入,翻倍浪费)

# Thinking / reasoning(deepseek-v4-pro)。经 extra_body 传,兼容各 SDK 版本。
# 思维链经 reasoning_content 返回;thinking 开启时 temperature/top_p/惩罚项全部失效。
#
# 按 stage 细分(任务复杂度差异大):
#   - Stage 2 局部摘要 = 抽取型简单任务,但调用量最大(N 段=N 次)→ **关 thinking** 省时省 token
#     (thinking 关时 temperature 重新生效,抽取更稳定)。
#   - Stage 3 阶段聚合 = 结构化归并 → 开 thinking,effort=high。
#   - Stage 4 根因判定 = 唯一需要深度因果推理(去偏逻辑全在此)→ 开 thinking,effort=max。
# llm_client.chat() 按 per-call `thinking`/`reasoning_effort` 决定(覆盖下面的全局回退)。
LLM_THINKING_ENABLED = True              # 全局回退默认(未显式指定 thinking 的调用沿用它)
LLM_REASONING_EFFORT_DEFAULT = "high"    # 全局回退默认 effort(向后兼容)

LLM_THINKING_LOCAL = False              # Stage 2 局部摘要:关 thinking(抽取型简单任务,占 84% 调用/成本,
LLM_REASONING_EFFORT_LOCAL = None       #   关后单文件耗时≈1/4;thinking 关 → effort 不读、temperature=0.1 重新生效)
LLM_THINKING_PHASE = True                # Stage 3 阶段聚合:开
LLM_REASONING_EFFORT_PHASE = "high"
LLM_THINKING_ROOT = True                 # Stage 4 根因判定:开
LLM_REASONING_EFFORT_ROOT = "high"       

# ----------------------------------------------------------------------------
# Segmentation
# ----------------------------------------------------------------------------
SEG_TARGET_CHARS = 24000        # 约 6k token
SEG_MAX_CHARS = 32000           # 硬上限
SEG_MIN_STEPS = 8
SEG_MIN_CHARS_FOR_SOFT_BOUNDARY = 10000   # agent_shift/finish 软切前要积累的最小字符数
SEG_MAX_STEPS = 80
SEG_OVERLAP_STEPS = 5

# ----------------------------------------------------------------------------
# Step / task text 长度
# ----------------------------------------------------------------------------
# Round 6:不再裁内容。deepseek-v4-pro 百万上下文 ≫ 任何阈值(整条最大轨迹 ~175k token),
# 一律向 prompt 发 content_full / 完整 question / 完整 verifier。只留一个极高安全帽防失控大 step。
STEP_FULL_MAX_CHARS = 200_000   # 单步>20万字符才截(纯防御,正常 step 远低于此,几乎不触发)

# 以下为旧"裁内容"常量,Round 6 起**不再用于裁 prompt 内容**;仅 STEP_HASH_* 仍用于指纹。
STEP_HASH_HEAD_CHARS = 200
STEP_HASH_TAIL_CHARS = 200
# 兼容保留(step_enricher 仍据此算 content_head/tail 备查,但不再喂 prompt):
STEP_HEAD_CHARS = 500
STEP_TAIL_CHARS = 500
# 兼容保留(已不再用于裁 task 文本):
TASK_QUESTION_CHARS = 2000
TASK_VERIFIER_CHARS = 1500
ROOT_VERIFIER_CHARS = 10000

# ----------------------------------------------------------------------------
# Taxonomy enums (validator/global/presenter use)
# ----------------------------------------------------------------------------
# 新分类: 5 主类 13 叶 + 兜底类 X(MAST 对齐)。标注=单 primary + 可选 contributing。
# code -> {main 大类字母, zh 中文叶子名}。CATEGORY_CODES 由本表派生。
CATEGORY_META: dict = {
    "A1_misunderstood_task":            {"main": "A", "zh": "误解任务/意图"},
    "A2_ignored_constraint":            {"main": "A", "zh": "无视约束"},
    "A3_misread_observation":           {"main": "A", "zh": "误读工具/终端结果"},
    "B1_hallucination":                 {"main": "B", "zh": "幻觉编造"},
    "B2_flawed_reasoning":              {"main": "B", "zh": "推理错误"},
    "C1_flawed_plan":                   {"main": "C", "zh": "计划缺陷"},
    "C2_wrong_handoff_or_role":         {"main": "C", "zh": "错误委派/角色"},
    "C3_context_or_state_loss":         {"main": "C", "zh": "上下文/状态丢失"},
    "D1_wrong_tool_or_args":            {"main": "D", "zh": "选错工具/参数"},
    "D2_unrecovered_tool_failure":      {"main": "D", "zh": "工具失败未恢复"},
    "D3_stuck_or_repetition":           {"main": "D", "zh": "重复/卡死"},
    "E1_verification_gap":              {"main": "E", "zh": "验证缺失/误判"},
    "E2_premature_or_wrong_completion": {"main": "E", "zh": "提前/错误完成"},
    "X1_underspecified_input":          {"main": "X", "zh": "输入欠定/自相矛盾"},
    "X2_unrecoverable_environment":     {"main": "X", "zh": "环境不可恢复"},
}
# 6 大类(含兜底类 X)中文名,供两级选择器/展示用
CATEGORY_MAIN_LABELS: dict = {
    "A": "理解输入", "B": "认知推理", "C": "规划协作",
    "D": "执行工具", "E": "验证收尾", "X": "非Agent责任",
}
# 上游优先 tie-break 次序(越靠前越"根"):A 理解 > B 认知 > C 规划 > D 执行 > E 验证 > X
CATEGORY_MAIN_PRIORITY = ["A", "B", "C", "D", "E", "X"]

CATEGORY_CODES = frozenset(CATEGORY_META)
CONFIDENCE_SET = frozenset({"high", "medium", "low"})
ROLE_SET = frozenset({"root_cause", "propagation", "exposure", "terminal"})
ALLOWED_PSEUDO_STEPS = frozenset({"system_evaluation"})
SPECIAL_AGENTS = frozenset({
    "SYSTEM", "TOOL", "PLATFORM", "ENVIRONMENT", "USER_INTENT_UNDERSPECIFIED",
})
SYSTEM_EVAL_STEP_SENTINEL = -1

LOCAL_FAILURE_TYPES = frozenset({
    "planning_error", "execution_error", "verifier_error",
    "tool_error", "communication_error", "none",
})
VERIFIER_RESULT_SET = frozenset({"PASS", "FAIL", "UNKNOWN"})

# ----------------------------------------------------------------------------
# Prompt versions
# ----------------------------------------------------------------------------
PROMPT_VERSIONS = {
    "local": "1.0",
    "phase": "1.0",
    "root": "1.5",   # 1.5: 需求↔官方测试矛盾→X1(非C1):§2.3.3 反事实硬验(纠正也翻不了盘→没资格当root)+ 判定表"忠实实现→C1"行内闸 + §5.y 详则(区分官方不可变测试 vs agent自写测试);X1 拓宽=输入欠定**或自相矛盾/过定**
                     # 1.4: 第三类计划缺陷(做法/数据源选错,违背任务明示要求→A2/C1)+ plan-vs-task 对照 + reason 证据绑定 + 全量不截断
}
SCHEMA_VERSION = "v2.0"
