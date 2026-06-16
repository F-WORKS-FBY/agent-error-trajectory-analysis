"""数据集 Profile:把"任意 MAS 轨迹格式"声明式地映射到内部规范 Step。

一个 `DatasetProfile` 描述三件事:
  1. **字段映射** —— history 在哪、每步的 step_id / role / agent 名 / content 在哪、
     任务文本(question/ground_truth/verifier...)在哪。
  2. **角色归一** —— 把框架各自的 agent 名归到 planner/executor/verifier/terminal/human,
     或 `passthrough`(每个原始名当作自身角色,适配完全任意的 MAS)。
  3. **委派/handoff 识别**(`DelegationSpec`)—— "这一步是不是把任务交给了别的 agent、交给了谁"。
     不同框架委派形式各异(名字后缀 `(-> X)` / 独立 to 字段 / 内容里的 transfer_to_agent(...) /
     纯轮流无委派),用可插拔策略覆盖。

设计要点:
  - `DEFAULT_PROFILE` **逐字复刻**改造前的行为(OpenHands+Magentic 角色集、`(-> X)` 委派、
    step 读 `step` 字段)→ 旧 5 bench 字节级不变。
  - 缺 `step` 字段时**按 index 枚举**(改造前会把这种步整步丢弃 → 空轨迹,这是 Who&When 跑不了的致命点)。
  - 没写 profile 时 `sniff_profile()` 自动嗅探常见字段兜底。
"""
from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional


# ----------------------------------------------------------------------------
# 默认角色集(= 改造前 step_enricher 的模块级常量,OpenHands + Magentic-One 并集)
# ----------------------------------------------------------------------------
DEFAULT_PLANNERS = {"DiagnostAgent", "Task_Planner"}
DEFAULT_EXECUTORS = {"ActionAgent", "Action_Expert"}
DEFAULT_VERIFIERS = {"JudgeAgent", "Verification_Expert"}
DEFAULT_TERMINALS = {"Computer_terminal"}
DEFAULT_HUMANS = {"human"}


# ----------------------------------------------------------------------------
# DelegationSpec —— 委派/handoff 的可插拔识别
# ----------------------------------------------------------------------------
@dataclass
class DelegationRule:
    """单条委派识别规则。strategy ∈ {name_regex, content_regex, field}。

    - name_regex / content_regex: `pattern` 必须含命名组 `(?P<target>...)`,分别对
      **agent 名** / **content** 匹配,命中即把该步视为委派、target 为接收方。
    - field: 从原始 step dict 的 `to_field` 取接收方(非空即为委派步)。
    """
    strategy: str
    pattern: Optional[str] = None
    to_field: Optional[str] = None

    def __post_init__(self) -> None:
        self._re = re.compile(self.pattern, re.I) if self.pattern else None

    def match(self, raw: Dict[str, Any], agent_name: str, content: str) -> Optional[str]:
        if self.strategy == "name_regex" and self._re is not None:
            m = self._re.search(agent_name or "")
            return m.group("target") if m else None
        if self.strategy == "content_regex" and self._re is not None:
            m = self._re.search(content or "")
            return m.group("target") if m else None
        if self.strategy == "field" and self.to_field:
            v = raw.get(self.to_field) if isinstance(raw, dict) else None
            return str(v) if v not in (None, "", []) else None
        return None


@dataclass
class DelegationSpec:
    """一组按序尝试的委派规则,第一个产出非空 target 的即采用。"""
    rules: List[DelegationRule] = field(default_factory=list)

    def resolve_target(self, raw: Dict[str, Any], agent_name: str, content: str) -> Optional[str]:
        for rule in self.rules:
            t = rule.match(raw, agent_name, content)
            if t:
                t = t.strip()
                if t:
                    return t
        return None

    def strip_sender(self, name: str) -> str:
        """去掉名字里的委派后缀,得到发起方裸名(仅对 name_regex 规则有意义)。

        如 'DiagnostAgent (-> ActionAgent)' -> 'DiagnostAgent';非名字内委派则原样返回。
        """
        n = name or ""
        for rule in self.rules:
            if rule.strategy == "name_regex" and rule._re is not None:
                stripped = rule._re.sub("", n).strip()
                if stripped and stripped != n:
                    return stripped
        return n


# 默认:名字后缀 `X (-> Y)`(复刻改造前;锚定结尾,与 global_reducer 的 target 抽取语义一致)。
_DEFAULT_NAME_DELEG_RE = r"\s*\(->\s*(?P<target>.+?)\)\s*$"

# 常见 handoff 内容写法(工具调用式 / 编排式)。**仅供需要的 profile 选用**,默认 profile 不含,
# 以免把旧 bench 里普通文本误判成委派(破坏字节级一致)。
_COMMON_CONTENT_DELEG_RE = (
    r"(?:transfer_to_agent|transfer_to|handoff_to|handoff|delegate_to|"
    r"assign(?:ed)?\s+to|next\s+speaker)\s*[:=(]?\s*[\"']?"
    r"(?P<target>[A-Za-z_][\w .\-]{1,60}?)[\"')]?\s*(?:$|[\n,.)])"
)


def default_delegation() -> DelegationSpec:
    return DelegationSpec(rules=[DelegationRule(strategy="name_regex", pattern=_DEFAULT_NAME_DELEG_RE)])


# ----------------------------------------------------------------------------
# DatasetProfile
# ----------------------------------------------------------------------------
@dataclass
class DatasetProfile:
    name: str = "default"

    # --- 字段映射 ---
    history_key: str = "history"
    step_id_field: Optional[str] = "step"      # 缺该字段(或值非整数)→ 按 index 枚举
    role_field: str = "role"
    agent_name_field: Optional[str] = "name"   # agent 名所在字段
    agent_from_role: bool = False              # True: agent 名取自 role_field(如 Who&When Hand-Crafted)
    content_field: str = "content"
    is_correct_field: str = "is_correct"

    # 任务文本字段(build_task_brief 用)
    question_field: str = "question"
    ground_truth_field: str = "ground_truth"
    verifier_field: str = "verifier_output"
    metadata_field: str = "metadata"
    agent_patch_field: str = "agent_patch"
    runtime_errors_field: str = "runtime_errors"

    # --- 角色归一 ---
    role_mode: str = "mapped"                  # "mapped" | "passthrough"
    planners: frozenset = field(default_factory=lambda: frozenset(DEFAULT_PLANNERS))
    executors: frozenset = field(default_factory=lambda: frozenset(DEFAULT_EXECUTORS))
    verifiers: frozenset = field(default_factory=lambda: frozenset(DEFAULT_VERIFIERS))
    terminals: frozenset = field(default_factory=lambda: frozenset(DEFAULT_TERMINALS))
    humans: frozenset = field(default_factory=lambda: frozenset(DEFAULT_HUMANS))

    # --- 委派 ---
    delegation: DelegationSpec = field(default_factory=default_delegation)

    @property
    def human_values(self) -> frozenset:
        return self.humans

    # -- 取值 helpers(供 step_enricher 调用)--
    def extract_agent_name(self, raw: Dict[str, Any]) -> str:
        if self.agent_from_role:
            return str(raw.get(self.role_field) or "").strip() or "unknown"
        if self.agent_name_field and raw.get(self.agent_name_field):
            return str(raw.get(self.agent_name_field)).strip()
        # 退回 role 字段,再退回 unknown
        rv = raw.get(self.role_field)
        if rv and not self.agent_from_role and str(rv) not in ("user", "assistant"):
            return str(rv).strip()
        return "unknown"

    def extract_message_role(self, raw: Dict[str, Any], agent_name: str) -> str:
        """归一到 'user' / 'assistant'(detect_action_type 用)。"""
        if self.agent_from_role:
            base = self.delegation.strip_sender(agent_name)
            lowered = {h.lower() for h in self.humans}
            return "user" if base.lower() in lowered else "assistant"
        # 非 agent_from_role:保留原始 role(复刻改造前 `raw.get('role') or 'assistant'`)
        return str(raw.get(self.role_field) or "assistant")

    def extract_content(self, raw: Dict[str, Any]) -> str:
        c = raw.get(self.content_field)
        if not isinstance(c, str):
            c = "" if c is None else str(c)
        return c

    def normalize_role(self, agent_name_raw: str) -> str:
        base = self.delegation.strip_sender(agent_name_raw or "").strip()
        if not base:
            return "unknown"
        # 已知基础设施角色无论哪种模式都先归一(终端/人类),保留可读性与启发式。
        if base in self.terminals:
            return "terminal"
        if base in self.humans:
            return "human"
        if self.role_mode == "passthrough":
            return base          # 每个原始名当作自身角色
        if base in self.planners:
            return "planner"
        if base in self.executors:
            return "executor"
        if base in self.verifiers:
            return "verifier"
        return "unknown"

    def resolve_delegate_target(self, raw: Dict[str, Any], agent_name: str, content: str) -> Optional[str]:
        return self.delegation.resolve_target(raw, agent_name, content)

    def clone(self, **overrides: Any) -> "DatasetProfile":
        return replace(self, **overrides)


DEFAULT_PROFILE = DatasetProfile(name="default")


# ----------------------------------------------------------------------------
# 内置 profiles
# ----------------------------------------------------------------------------
# Who&When:两子集同处理(step 枚举、passthrough 保留各 agent 名以利 who 归因);
# 委派同时支持名字后缀(Hand-Crafted 的 'Orchestrator (-> WebSurfer)')与内容式 handoff。
_WHO_WHEN_DELEG = DelegationSpec(rules=[
    DelegationRule(strategy="name_regex", pattern=_DEFAULT_NAME_DELEG_RE),
    DelegationRule(strategy="content_regex", pattern=_COMMON_CONTENT_DELEG_RE),
])

WHO_WHEN_PROFILE = DatasetProfile(
    name="who_and_when",
    step_id_field=None,            # 无 step 字段 → 枚举
    role_mode="passthrough",
    delegation=_WHO_WHEN_DELEG,
    humans=frozenset({"human", "user"}),
    terminals=frozenset({"Computer_terminal", "ComputerTerminal"}),
)

BUILTIN_PROFILES: Dict[str, DatasetProfile] = {
    "default": DEFAULT_PROFILE,
    "who_and_when": WHO_WHEN_PROFILE,
    "who&when": WHO_WHEN_PROFILE,
}


# ----------------------------------------------------------------------------
# 自动嗅探兜底
# ----------------------------------------------------------------------------
_HISTORY_KEYS = ("history", "messages", "trajectory", "conversation", "steps")
_CONTENT_KEYS = ("content", "text", "message", "value")


def _first_present(d: Dict[str, Any], keys) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, list) and v:
            return k
    return None


def sniff_profile(data: Dict[str, Any]) -> DatasetProfile:
    """没指定 profile 时,从一条原始数据嗅探常见字段,返回基于 default 覆盖的 profile。"""
    overrides: Dict[str, Any] = {"name": "sniffed"}

    hk = _first_present(data, _HISTORY_KEYS) or "history"
    overrides["history_key"] = hk
    hist = data.get(hk) or []
    sample = next((x for x in hist if isinstance(x, dict)), {})

    # step 字段:有整型 step 才用,否则枚举
    if not isinstance(sample.get("step"), int):
        overrides["step_id_field"] = None

    # content 字段
    for ck in _CONTENT_KEYS:
        if ck in sample:
            overrides["content_field"] = ck
            break

    # agent 名位置:有 name → 用 name;否则若 role 存在且非 user/assistant → agent 名在 role 里
    if sample.get("name"):
        overrides["agent_name_field"] = "name"
    else:
        rv = sample.get("role")
        if rv is not None and str(rv) not in ("user", "assistant"):
            overrides["agent_from_role"] = True
            overrides["role_mode"] = "passthrough"
            overrides["humans"] = frozenset({"human", "user"})

    return DEFAULT_PROFILE.clone(**overrides)


def _profile_from_dict(d: Dict[str, Any]) -> DatasetProfile:
    """从 JSON dict 构造 profile(委派/角色集做类型转换)。"""
    d = dict(d)
    deleg = d.pop("delegation", None)
    for key in ("planners", "executors", "verifiers", "terminals", "humans"):
        if key in d and d[key] is not None:
            d[key] = frozenset(d[key])
    prof = DEFAULT_PROFILE.clone(**d)
    if deleg is not None:
        rules = [DelegationRule(**r) for r in (deleg.get("rules") or [])]
        prof = prof.clone(delegation=DelegationSpec(rules=rules))
    return prof


def resolve_profile(name_or_path: Optional[str], data: Dict[str, Any]) -> DatasetProfile:
    """解析 profile:内置名 → 用之;`.json` 路径 → 加载;否则 → 嗅探兜底。"""
    if not name_or_path:
        return sniff_profile(data)
    key = name_or_path.strip()
    if key.lower() in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[key.lower()]
    p = Path(key)
    if p.suffix.lower() == ".json" and p.exists():
        return _profile_from_dict(json.loads(p.read_text(encoding="utf-8")))
    # 未知名:退回嗅探(不报错,保持鲁棒)
    return sniff_profile(data)
