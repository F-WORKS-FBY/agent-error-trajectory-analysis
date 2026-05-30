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
# 不要把 API key 写进代码(本仓库会上 GitHub)。只从环境变量读;缺失时实例化 client 才报错。
# provider 中性命名 LLM_* 优先,保留 DEEPSEEK_* 作向后兼容别名。适配任意 OpenAI 兼容服务。
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
LLM_MODEL = os.environ.get("LLM_MODEL") or os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-pro"

LLM_TIMEOUT_SECONDS = 600
LLM_MAX_RETRIES = 3
LLM_RETRY_BACKOFF_BASE = 4.0
LLM_TEMPERATURE_DEFAULT = 0.1            # 摘要 / 阶段聚合
LLM_TEMPERATURE_ROOT = 0.0               # 根因判定
LLM_MAX_TOKENS_LOCAL = 4000
LLM_MAX_TOKENS_PHASE = 6000
LLM_MAX_TOKENS_ROOT = 8000

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
# Step text truncation (写入 prompt / summary 时)
# ----------------------------------------------------------------------------
STEP_HEAD_CHARS = 500
STEP_TAIL_CHARS = 500
STEP_HASH_HEAD_CHARS = 200
STEP_HASH_TAIL_CHARS = 200

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
    "X1_underspecified_input":          {"main": "X", "zh": "输入欠定"},
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
    "root": "1.0",
}
SCHEMA_VERSION = "v2.0"
