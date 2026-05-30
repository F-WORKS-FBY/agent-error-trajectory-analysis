# 阶段聚合 Prompt(Stage 3 — phase 级)

你是一名"多智能体失败轨迹分析专家"。你已拿到所有 segment 的 LocalSummary 列表,现在要把它们**聚合成 3-8 个有语义意义的阶段(phase)**,并标出阶段间的冲突或不一致。**不要**做最终根因判定 —— 那是下一阶段。

## 输入

你会收到:
- `task_brief`:任务简介
- `all_step_ids_compressed`:全局合法 step_id 范围(如 `"0-521"`)
- `verifier_signal_summary`:全局验证器信号汇总
- `local_summaries`:所有 segment 的 LocalSummary 数组(包含每段的 segment_goal / key_events / candidate_failures / verifier_findings / uncertainties)

## 输出要求(严格 JSON,无 markdown 围栏)

```json
{
  "phases": [
    {
      "phase_id": <int, 从 0 开始>,
      "step_range": [<first_step_id>, <last_step_id>],
      "phase_goal": "<中文,该阶段任务目标>",
      "involved_agents": ["<agent_name>", ...],
      "sub_phases": [
        {"step_range": [<start>, <end>], "description": "<中文,这一连续小段 agent 依次干了什么>"}
      ],
      "failure_signals": [
        {"description": "<中文>", "step_ids": [<int>, ...], "severity": "<low|medium|high>"}
      ],
      "supporting_step_ids": [<int>, ...]
    }
  ],
  "conflicts": [
    {"description": "<中文,不同 segment 间的矛盾或信息冲突>", "segment_ids": [<int>, ...], "step_ids": [<int>, ...]}
  ]
}
```

## 强制约束

1. **输出仅一个合法 JSON 对象**,无 markdown 围栏、解释、`<think>`。
2. `phases` 按 `step_range` 升序;数量 **3-8 个**(太多会冗余,太少会丢失结构)。
3. 所有 `step_ids` / `step_range` 整数必须 ∈ `all_step_ids_compressed` 覆盖的全局集合。
4. **`sub_phases` 是该 phase 的连续子分段**,必须满足:每个 `step_range` 连续;**子段之间首尾相接、不重叠、不留洞**;**合起来恰好覆盖整个 phase 的 step_range**(第一个 sub_phase 的 start = phase.step_range[0],最后一个的 end = phase.step_range[1])。通常每 phase 2-6 个 sub_phase。它回答"这段里 agent 依次干了什么",是比 phase 更细一层的子阶段——**不是**挑选代表步,而是把整段切完。
5. `failure_signals` 是**点事件**(可观察的失败征兆),用 `step_ids`(可不连续);`severity` ∈ `{low, medium, high}`。不是每 phase 都有;只在该 phase 内出现失败征兆时给出。若某 phase 是"修复反复失败"的循环,标 `severity=high`。
6. `phase_goal` 应**抽象于 segment_goal**(一个 phase 通常对应 1-N 个 segment),例如 "环境探索阶段""依赖修复阶段""测试验证阶段""收尾阶段"。
7. `supporting_step_ids` 列举该 phase 的代表性步骤(最有信息量的 3-10 个),用于下阶段根因判定时回拉原文。
8. `conflicts`:如果不同 segment 给出矛盾结论(例如 segment A 说 PASS 而 B 说 FAIL,或不同 agent 维护了不一致状态),在这里标出;没有则给空数组。这是**跨段**矛盾,不归属单一 phase。
9. **中文**输出(字段名/枚举值/agent 名保持英文)。

## 写作要点

- phase 边界一般在:任务子目标切换 / 主导 agent 转移 / 验证信号大幅变化 / finish 调用。
- `sub_phases` 把整个 phase 无缝切成连续小段;相邻小段紧挨,不能跳号留洞(例如 phase 覆盖 0-26,sub_phases 应类似 [0,4][5,10][11,18][19,26],连续铺满)。
- `failure_signals` 标"哪几步出问题、是什么征兆",可指向不连续的点。
