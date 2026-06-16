"""DeepSeek (OpenAI-compatible) client。

从 v1 `run_llm_annotate.py` 提炼:
- 重试 / 退避(network error)
- finish_reason=length 自动加 max_tokens 重试一次
- think / markdown 围栏剥离
- JSON 解析 + 非法反斜杠修复
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

try:
    from openai import OpenAI
except ImportError:                                                       # pragma: no cover
    OpenAI = None                                                          # type: ignore

from .. import config

LOG = logging.getLogger("MAS_trajectory_analysis.llm")


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_VALID_ESCAPE_NEXT = set('"\\/bfnrtu')


def _infer_thinking_style(base_url: str) -> str:
    """按 base_url 判断该用哪种 thinking 风格(见 config.LLM_THINKING_STYLE 说明)。"""
    if config.LLM_THINKING_STYLE:
        return config.LLM_THINKING_STYLE
    b = (base_url or "").lower()
    if "deepseek.com" in b:
        return "deepseek"
    if "siliconflow" in b:
        # SiliconFlow 上很多模型(如 DeepSeek-V3)非推理模型,贸然发私有参数易 400 → 默认不注入。
        return "none"
    return "none"


# ----------------------------------------------------------------------------
# Error classification (reused from v1)
# ----------------------------------------------------------------------------
def _is_token_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    keywords = [
        "context_length_exceeded", "maximum context length", "too many tokens",
        "token limit", "context window", "request too large", "payload too large",
        "content_length_limit", "reduce the length", "input is too long", "exceed",
    ]
    return any(kw in msg for kw in keywords)


def _is_retryable_network_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    cls = exc.__class__.__name__.lower()
    keywords = [
        "timeout", "timed out", "connection", "rate limit", "ratelimit", "429",
        "too many requests", "500", "502", "503", "504",
        "internal server error", "bad gateway", "service unavailable",
        "gateway timeout", "remote disconnected", "incomplete read", "ssl", "eof",
    ]
    if any(kw in msg for kw in keywords):
        return True
    if any(kw in cls for kw in ["timeout", "connection", "apierror", "ratelimit"]):
        return True
    return False


# ----------------------------------------------------------------------------
# JSON cleanup (reused from v1)
# ----------------------------------------------------------------------------
def _strip_think_blocks(text: str) -> str:
    result = _THINK_RE.sub("", text).strip()
    if not result and "<think>" in text:
        idx = text.rfind("</think>")
        if idx != -1:
            result = text[idx + len("</think>"):].strip()
        else:
            idx = text.find("<think>")
            result = text[:idx].strip()
    return result


def _repair_illegal_backslashes(text: str) -> str:
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] not in _VALID_ESCAPE_NEXT:
            out.append("\\\\")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _extract_usage(usage: Any) -> Dict[str, Optional[int]]:
    """防御式从 OpenAI 兼容 response.usage 取 token 数(各 provider 字段略有差异)。"""
    if usage is None:
        return {"prompt_tokens": None, "completion_tokens": None,
                "reasoning_tokens": None, "total_tokens": None}
    pt = getattr(usage, "prompt_tokens", None)
    ct = getattr(usage, "completion_tokens", None)
    tt = getattr(usage, "total_tokens", None)
    rt = None
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None:
        rt = getattr(details, "reasoning_tokens", None)
        if rt is None and isinstance(details, dict):
            rt = details.get("reasoning_tokens")
    if rt is None:
        rt = getattr(usage, "reasoning_tokens", None)
    return {"prompt_tokens": pt, "completion_tokens": ct,
            "reasoning_tokens": rt, "total_tokens": tt}


def parse_json_response(raw: str) -> Optional[Dict[str, Any]]:
    """同 v1 _parse_llm_output。返回 dict 或 None。"""
    text = (raw or "").strip()
    if "<think>" in text:
        text = _strip_think_blocks(text)
    if text.startswith("```"):
        first_nl = text.index("\n") if "\n" in text else 3
        text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        candidate = text[brace_start: brace_end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            repaired = _repair_illegal_backslashes(candidate)
            if repaired != candidate:
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    pass
    return None


# ----------------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------------
class DeepSeekClient:
    def __init__(
        self,
        api_key: str = config.LLM_API_KEY,
        base_url: str = config.LLM_BASE_URL,
        model: str = config.LLM_MODEL,
        timeout: int = config.LLM_TIMEOUT_SECONDS,
        thinking_style: Optional[str] = None,
    ) -> None:
        if OpenAI is None:
            raise RuntimeError(
                "openai package missing. pip install openai>=1.0"
            )
        if not api_key:
            raise RuntimeError(
                "缺少 API key。请设置环境变量 LLM_API_KEY(或 --dry-run 跳过 LLM)。\n"
                "  export LLM_API_KEY=sk-...\n"
                "换其它 OpenAI 兼容服务:再设 LLM_BASE_URL / LLM_MODEL(或用 --base-url / --model)。\n"
                "(向后兼容:也接受 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL。)"
            )
        self.model = model
        # thinking 风格:deepseek / siliconflow / none。决定 chat() 注入哪种(或不注入)私有参数。
        self.thinking_style = thinking_style or _infer_thinking_style(base_url)
        self._tls = threading.local()        # 每线程保存最近一次的 reasoning_content / usage(供 --workers 下安全取用)
        # 可选的指标接收器:设为 list 后,每次调用会 append 一条 {stage, latency_s, *_tokens} 记录。
        # 默认 None → 生产路径零开销、无内存泄漏;基准/审计时由调用方临时挂上。
        self.metrics_sink: Optional[list] = None
        self._client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=0,
        )

    @property
    def last_reasoning_content(self) -> str:
        """最近一次调用(本线程)返回的思维链;未开 thinking 或不支持时为空串。"""
        return getattr(self._tls, "last_reasoning", "") or ""

    @property
    def last_usage(self) -> Dict[str, Any]:
        """最近一次调用(本线程)的 token/latency 记录;无则空 dict。"""
        return getattr(self._tls, "last_usage", None) or {}

    def chat(
        self,
        system: str,
        user: str,
        temperature: float = config.LLM_TEMPERATURE_DEFAULT,
        max_tokens: int = config.LLM_MAX_TOKENS_LOCAL,
        reasoning_effort: Optional[str] = None,
        thinking: Optional[bool] = None,
        stage: Optional[str] = None,
    ) -> Tuple[str, str]:
        # thinking 经 extra_body 传(兼容各 SDK 版本);effort 控推理强度。
        # per-call `thinking` 覆盖全局 LLM_THINKING_ENABLED;关时显式发 disabled(deepseek 默认开)。
        # 私有参数按 thinking_style 注入:非 deepseek 端点默认**不注入**,避免 400。
        thinking_on = config.LLM_THINKING_ENABLED if thinking is None else thinking
        extra_body: Dict[str, Any] = {}
        if self.thinking_style == "deepseek":
            if thinking_on:
                extra_body["thinking"] = {"type": "enabled"}
                if reasoning_effort:
                    extra_body["reasoning_effort"] = reasoning_effort
            else:
                extra_body["thinking"] = {"type": "disabled"}
        elif self.thinking_style == "siliconflow":
            # SiliconFlow 混合推理模型用 enable_thinking;非推理模型会忽略该字段。
            extra_body["enable_thinking"] = bool(thinking_on)
        # else "none": 不注入任何私有 thinking 参数(temperature 照常生效)。
        last_exc: Optional[Exception] = None
        for attempt in range(config.LLM_MAX_RETRIES):
            try:
                t0 = time.perf_counter()
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                    extra_body=extra_body,
                )
                latency_s = time.perf_counter() - t0
                choice = resp.choices[0]
                text = choice.message.content or ""
                reasoning = getattr(choice.message, "reasoning_content", None) or ""
                self._tls.last_reasoning = reasoning
                if reasoning:
                    LOG.debug("reasoning_content len=%d (effort=%s)", len(reasoning), reasoning_effort)
                finish = choice.finish_reason or "unknown"
                # 采集 token/latency:存 thread-local last_usage,并(若挂了 sink)记一条。
                rec = {
                    "stage": stage,
                    "thinking": thinking_on,
                    "effort": reasoning_effort if thinking_on else None,
                    "latency_s": latency_s,
                    "finish": finish,
                    **_extract_usage(getattr(resp, "usage", None)),
                }
                self._tls.last_usage = rec
                if self.metrics_sink is not None:
                    self.metrics_sink.append(rec)
                return text, finish
            except Exception as e:
                last_exc = e
                if _is_token_limit_error(e):
                    raise
                if attempt < config.LLM_MAX_RETRIES - 1 and _is_retryable_network_error(e):
                    delay = config.LLM_RETRY_BACKOFF_BASE * (2 ** attempt)
                    LOG.warning(
                        "network error (attempt %d/%d): %s; retry in %.1fs",
                        attempt + 1, config.LLM_MAX_RETRIES, str(e)[:200], delay,
                    )
                    time.sleep(delay)
                    continue
                raise
        if last_exc:
            raise last_exc
        return "", "unknown"

    def chat_json(
        self,
        system: str,
        user: str,
        temperature: float = config.LLM_TEMPERATURE_DEFAULT,
        max_tokens: int = config.LLM_MAX_TOKENS_LOCAL,
        retry_on_length: bool = True,
        reasoning_effort: Optional[str] = None,
        thinking: Optional[bool] = None,
        stage: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], str, str]:
        """返回 (parsed_dict_or_None, raw_text, finish_reason)。思维链可经 last_reasoning_content 取。"""
        raw, finish = self.chat(
            system, user, temperature=temperature, max_tokens=max_tokens,
            reasoning_effort=reasoning_effort, thinking=thinking, stage=stage,
        )
        if finish == "length" and retry_on_length:
            LOG.warning("output truncated (length); retry with 2x max_tokens")
            raw, finish = self.chat(
                system, user, temperature=temperature,
                max_tokens=min(max_tokens * 2, 32768),
                reasoning_effort=reasoning_effort, thinking=thinking, stage=stage,
            )
        parsed = parse_json_response(raw)
        return parsed, raw, finish


# ----------------------------------------------------------------------------
# 每阶段 client 工厂
# ----------------------------------------------------------------------------
def build_stage_clients(
    model_override: Optional[str] = None,
    base_url_override: Optional[str] = None,
) -> Dict[str, "DeepSeekClient"]:
    """按 config.STAGE_LLM 为 local/phase/root 各建一个 client,
    对相同 (model, base_url, api_key) 三元组**去重共享同一实例**(常见单模型场景仍只建 1 个)。

    model_override / base_url_override(来自 CLI --model / --base-url):**全局覆盖所有阶段**,
    保持旧的单 client 行为。
    """
    cache: Dict[tuple, "DeepSeekClient"] = {}
    out: Dict[str, "DeepSeekClient"] = {}
    for stage, cfg in config.STAGE_LLM.items():
        model = model_override or cfg["model"]
        base_url = base_url_override or cfg["base_url"]
        api_key = cfg["api_key"]
        key = (model, base_url, api_key)
        if key not in cache:
            cache[key] = DeepSeekClient(api_key=api_key, base_url=base_url, model=model)
        out[stage] = cache[key]
    return out
