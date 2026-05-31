# MAS_trajectory_analysis — 多 Agent 失败轨迹根因分析(LLM 先行标注)

把 500–800 步的多 Agent 失败轨迹,经 **分段 → 局部摘要 → 阶段聚合 → 根因判定** 四级流水线,
产出一份**精简、可直接挂到标注平台**的展示型 JSON:任务目标、阶段时间线(可下钻看每段子步与异常信号)、
以及一个带失败链/证据/置信度的根因建议。每个 step_id 都强制回校到原轨迹,杜绝编造。

## 安装

```bash
pip install -r requirements.txt          # 仅需 openai>=1.0
export LLM_API_KEY=sk-...                 # 必填(或 cp .env.example .env 后填)
# 选填:换任意 OpenAI 兼容服务(默认是 DeepSeek)
export LLM_BASE_URL=https://api.deepseek.com
export LLM_MODEL=deepseek-v4-pro
# 也可不用 env,在命令行临时覆盖:--base-url ... --model ...
# 向后兼容:DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL 仍可用(LLM_* 优先)
```

## 快速开始

```bash
# 扁平目录(你自己的 bench:一个目录下放一堆 *.json)
python -m MAS_trajectory_analysis.run --input-dir /path/to/your_bench --output-dir /path/to/out

# 只跑 1 个文件 + 输出完整中间结果(审计用 <name>.debug.json)
python -m MAS_trajectory_analysis.run --input-dir /path/to/your_bench --output-dir /path/to/out \
       --file some_trace.json --debug-sidecar -v

# 经典布局(<input-dir>/<benchmark>/*.json),并发 4
python -m MAS_trajectory_analysis.run --input-dir ./Who\&When_style --benchmark swe_bench_pro \
       --output-dir ./out --workers 4

# 冒烟:不调 LLM,只看分段是否合理(不需要 API key)
python -m MAS_trajectory_analysis.run --input-dir /path/to/your_bench --dry-run --limit 3
```

## 输入格式契约

每个输入 JSON 至少需要:

| 字段 | 说明 | 必需 |
|---|---|---|
| `history` | step 数组,每项 `{step:int, role, name, content}` | ✅ |
| `is_correct` | 布尔(失败轨迹应为 false;true 会被跳过) | ✅ |
| `question` | 任务描述(没有则用 `ground_truth`/`task_brief` 兜底) | 建议 |
| `ground_truth` | str 或 dict(dict 可含 `benchmark`/`description`/期望校验项) | 建议 |
| `verifier_output` | 评测器输出(用于提取"为什么失败");缺失也能跑 | 可选 |
| `metadata` | `{benchmark, task_name, model}`;缺失则从 `ground_truth.benchmark`/文件名前缀推断 | 可选 |
| `mistake_agent/step/reason` | 人工标注槽(留空即可,输出会注入 `llm_mistake_*` 镜像) | 可选 |

### agent 角色对照(两类框架)

| 角色 | OpenHands(swe_bench_pro / terminal_bench_2) | Magentic-One(travelplanner / vitabench / webarena) |
|---|---|---|
| 规划/协调 planner | `DiagnostAgent` | `Task_Planner` |
| 执行 executor | `ActionAgent` | `Action_Expert` |
| 验证 verifier | `JudgeAgent` | `Verification_Expert` |
| 环境/工具返回 | `Computer_terminal` | `Computer_terminal` |
| 用户 | `human` | (任务在 step0 给出) |
| **委派态(独立可追责)** | `DiagnostAgent (-> ActionAgent)` / `(-> JudgeAgent)` | (无) |

**委派态名是独立 agent**:`X (-> Y)` 代表"X 发起委派给 Y 的那一步";当根因落在委派的指令/计划里时,责任 agent 就用这个名字(与标注平台的 agent 选项一致)。`valid_agents` 用原始名(含委派态)喂给 LLM。
其它框架的名字会落 `unknown`(不致命,仅影响分段启发式)——如需精确识别,在
[preprocess/step_enricher.py](preprocess/step_enricher.py) 的 `PLANNERS/EXECUTORS/VERIFIERS` 集合里加上即可。

## 输出格式(精简、纯展示型)

输出 = 原 JSON 字节级一致 + 顶层注入 **4 个字段**:

```jsonc
{
  // ... 原 JSON 全部字段保留 ...
  "llm_mistake_agent": "ActionAgent",          // 镜像 Who&When 的 mistake_agent(供评测对比)
  "llm_mistake_step": 38,                        // 镜像 mistake_step(int;-1=system_evaluation 虚拟步)
  "llm_mistake_reason": "<完整根因解释>",         // = root_cause.reason(唯一一份)
  "llm_analysis_summary": {                      // 平台只读这一块
    "meta": {schema_version, timestamp, model, benchmark, task_name, n_steps, n_phases},
    "task_goal": {is_correct, brief, verdict_line},
    "phases": [{
      "phase_id", "step_range", "phase_goal", "involved_agents",
      "sub_phases": [{"step_range":[0,4], "description":"..."}],   // 连续铺满该 phase,无断点
      "anomaly_signals": [{"kind","description","step_ids","severity"}]  // 点事件(失败征兆)
    }],
    "cross_phase_conflicts": [{"description","step_ids"}],          // 跨段矛盾(声称成功但后续失败)
    "root_cause": {                              // 唯一的结构化根因(最全)
      "agent","step",
      "primary_category": {code, zh, main, main_label},
      "contributing_factors": [{code, zh, main, main_label}],
      "reason": "<完整解释,唯一一份>",
      "failure_chain": [{step, agent, role, description}],   // root_cause→propagation→exposure→terminal
      "confidence","confidence_reason","evidence_step_ids",
      "counterfactual","expert_review_hints",
      "abstain","needs_human_review","banner"
    },
    "category_legend": {"<code>": {zh, main, main_label}},   // 给表单的类别选择器
    "step_ref_index": {"<id>": {step_hash, agent, action_type}},  // step 跳转/防漂移锚定
    "ai_summary_markdown": "...",                // 渲染兜底:AI 总结
    "root_cause_markdown": "..."                 // 渲染兜底:根因建议
  }
}
```

`--debug-sidecar` 会另写 `<name>.debug.json`,内含完整中间结果(`segments`/`local_summaries`/原始 `phases` 等),仅供审计,不进主输出。

平台展示约定:phase 时间线 → 点开某 phase → 上半区按 `sub_phases` 看"依次干了什么"(点 range 跳转),
下半区按 `anomaly_signals` 红/黄高亮"异常信号步";右侧 LLM 建议面板读 `root_cause`,
`failure_chain` 帮标注员区分"最初根因"vs"失败显露处"。

## 根因分类 Taxonomy(5 主类 13 叶 + 兜底类 X;单主因 + 可选次因)

| code | 主类 | 中文 | 何时选 |
|---|---|---|---|
| `A1_misunderstood_task` | A 理解输入 | 误解任务/意图 | 没读懂要做什么 |
| `A2_ignored_constraint` | A | 无视约束 | 约束就在任务里却违反 |
| `A3_misread_observation` | A | 误读工具/终端结果 | 事实在眼前但读错 |
| `B1_hallucination` | B 认知推理 | 幻觉编造 | 编造事实/API/文件 |
| `B2_flawed_reasoning` | B | 推理错误 | 输入对但推错 |
| `C1_flawed_plan` | C 规划协作 | 计划缺陷 | 分解/排序/方案错 |
| `C2_wrong_handoff_or_role` | C | 错误委派/角色 | 派错 agent / 角色失职 |
| `C3_context_or_state_loss` | C | 上下文/状态丢失 | 信息没接住 / 状态不同步 |
| `D1_wrong_tool_or_args` | D 执行工具 | 选错工具/参数 | 错在单步动作 |
| `D2_unrecovered_tool_failure` | D | 工具失败未恢复 | 该恢复没恢复 |
| `D3_stuck_or_repetition` | D | 重复/卡死 | 原地打转无进展 |
| `E1_verification_gap` | E 验证收尾 | 验证缺失/误判 | 漏验/验错 |
| `E2_premature_or_wrong_completion` | E | 提前/错误完成 | 没达成就 finish |
| `X1_underspecified_input` | X 非Agent | 输入欠定/自相矛盾 | 锅在输入(信息不足 或 约束互相矛盾/无解)|
| `X2_unrecoverable_environment` | X | 环境不可恢复 | 锅在环境(慎用) |

**多因共存**:`primary_category` 单选(决定性错误那一类);因果链下游进 `failure_chain` 不进类别;真正独立并存的次因才进 `contributing_factors`。同一步像两类时上游优先 `A>B>C>D>E`。
**X2 / D2 边界**:terminal-bench / SWE-bench-pro 故意把缺包缺依赖作为测试内容 → agent 应自己 pip install;没装**不选 X2**,改选 A/B/D3。详见 [prompts/root_cause.md](prompts/root_cause.md) §5.x。
历史用旧 10 类/17 类标注的数据,可用 [data/categories_legacy_map.json](data/categories_legacy_map.json) 的 `legacy10_to_new` / `v2_17_to_new` 回填。

## 目录布局

```
MAS_trajectory_analysis/
├── README.md  requirements.txt  .env.example  .gitignore
├── config.py                    # 常量 / API(env) / 路径 / 超参 / Taxonomy
├── run.py                       # CLI 入口(python -m MAS_trajectory_analysis.run)
├── core/        schema.py, llm_client.py
├── preprocess/  loader.py, step_enricher.py, segmenter.py
├── analyze/     local_summarizer.py, global_reducer.py, validator.py
├── output/      presenter.py(展示层), io_writer.py(拷贝+注入+原子写)
├── prompts/     local_summary.md, phase_aggregate.md, root_cause.md
├── data/        categories_legacy_map.json
├── tools/       verify_diff.py(校验输出除注入字段外与原文字节一致)
└── logs/        运行日志(.gitignore)
```

## CLI 参数

```
python -m MAS_trajectory_analysis.run [-h]
   --input-dir DIR     # 输入目录;无 --benchmark 则读 DIR/*.json(扁平),有则读 DIR/<benchmark>/*.json
   --output-dir DIR    # 输出目录(镜像输入文件名)
   [--benchmark NAME]  # 经典布局的 bench 子目录名;扁平目录可省略
   [--file NAME]       # 只跑指定文件名
   [--limit N]         # 只处理前 N 个文件
   [--workers W]       # 文件级并发数(默认 1;批量跑用 --workers 8)
   [--overwrite]       # 覆盖已存在的输出
   [--debug-sidecar]   # 额外写 <name>.debug.json(完整中间结果)
   [--dry-run]         # 只跑分段,不调 LLM(无需 API key)
   [--model NAME]      # 覆盖 LLM 模型(默认 env LLM_MODEL)
   [--base-url URL]    # 覆盖 LLM base_url(默认 env LLM_BASE_URL)
   [--verbose]         # DEBUG 日志
```

**并发**:`--workers 8` 做文件级并发(同时跑 8 个文件,文件内部 stage 仍顺序)。各线程写不同文件、io_writer 用文件锁+原子写,线程安全。8 路并发即同时 ~8 路 LLM 请求,注意服务端速率限制(已有重试退避)。

## 防漂移机制

| 机制 | 位置 |
|---|---|
| `step_hash = sha1(step_id\|name\|content_head\|content_tail)[:16]` | `preprocess/step_enricher.py` |
| 所有 `step_id` / `evidence_step_ids` 强制 ∈ 全局 step 集合 | `analyze/validator.py` |
| `sub_phases` 连续铺满 phase(确定性修补) | `output/presenter.py` |
| Agent 名 ∈ trajectory.unique_names ∪ SPECIAL_AGENTS | `analyze/validator.py` |
| 证据不足时 `abstain=true` + `needs_human_review=true` + 横幅 | `run.py` |
| 校验失败 → `previous_errors` 提示重生成 1 次 | `run.py` |
