# -*- coding: utf-8 -*-
"""
探活层：对"当前生效出口"发真实 HTTP 请求，判断平台是否认这个出口 IP。

设计约束（重要）：
    本项目不改 OpenClash 配置（代理商节点会变，手改必失效），
    也不做"逐节点探活"（那需要把生产策略组反复切来切去，会打断用户流量）。

    因此探活只做一件事：**验证当前出口**。
    逐节点筛选由 provider 的延迟数据 + Jev 的地区先验完成，
    切换后立即探活验证，不通过则回滚 —— 见 engine.py 的 verify-and-rollback。

判据 v2（2026-09 重写：结构化错误信封解析，不再全文扫关键词）：
    Google API 错误体固定为 {"error": {"code", "status", "message"}}。
    优先解析信封字段，按短语精确匹配，顺序：geo -> abuse -> quota -> auth：

        geo_blocked     "User location is not supported..." 等地区文案
                        （注意：可能是 400 FAILED_PRECONDITION，也可能是 403！）
        ip_flagged      "automated queries" / "blocked due to abuse" 等滥用封禁
        quota_limited   429 / RESOURCE_EXHAUSTED（共享 IP 常态，不该拉黑）
        auth_ok         "unregistered callers" / "API key not valid" —— IP 已放行
        ok              2xx（带 key 时看 candidates）
        unknown         无法判别 —— 留证、验证不通过，但 **绝不直接拉黑**

    v1 的教训：400 一律当 auth_ok 会把最常见的地区风控
    （400 FAILED_PRECONDITION "User location is not supported"）判成"IP 已放行"；
    403 全文扫 "location/region" 又会误命中字段名。两种误判都会造成实际故障。

探针三级（按配置递进）：
    1. edge   无 key：GET /v1beta/models            —— 只能证明"边缘收了请求"
    2. key    GEMINI_API_KEY：POST :generateContent —— 真正回答"Gemini 认不认这个出口"
    3. shadow 经旁路 HTTP 监听（Mihomo listener 绑定固定组）—— 可对任意节点预检；
              切到 DIRECT 时即为"家庭宽带对照组"（L3 判据仲裁用）
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

# ---------- 判据短语（全部用多词精确短语，杜绝 "location/region" 单词误命中） ----------
GEO_MARKERS = (
    "user location is not supported",
    "location is not supported for the api use",
    "not available in your country",
    "not supported in your country",
    "country is not supported",
    "region is not supported",
    "unsupported region",
    "not available for use in your",
)
ABUSE_MARKERS = (
    "automated queries",
    "unusual traffic",
    "suspicious activity",
    "blocked due to abuse",
    "due to abuse",
    "have been blocked",
    "has been blocked",
    "temporarily blocked",
    "ip address blocked",
)
QUOTA_MARKERS = (
    "resource has been exhausted",
    "quota",
    "rate limit",
    "too many requests",
    "exceeded",
)
AUTH_MARKERS = (
    "api key", "api_key", "apikey", "unregistered", "credential",
    "authentication", "unauthorized", "identity", "oauth", "consumer",
    "permission to access", "caller",
)

PASSED_KINDS = ("ok", "auth_ok", "quota_limited")
BLOCKED_KINDS = ("geo_blocked", "ip_flagged")

DEFAULT_KEY_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
                   "gemini-2.0-flash-lite:generateContent")
KEY_PROBE_BODY = {
    "contents": [{"parts": [{"text": "ping"}]}],
    "generationConfig": {"maxOutputTokens": 1},
}


def _envelope(body: str | None) -> tuple[str, str]:
    """解析 Google RPC 错误信封 -> (error.status, error.message)。"""
    if not body:
        return "", ""
    try:
        obj = json.loads(body)
    except Exception:
        return "", body
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict):
            return str(err.get("status") or ""), str(err.get("message") or "")
        # 200 正常体（generateContent）没有 error
        return "", body if isinstance(body, str) else json.dumps(obj)[:400]
    return "", body


def _hit(ml: str, markers) -> str:
    for m in markers:
        if m in ml:
            return m
    return ""


def classify_response(status: int | None, body: str | None) -> tuple[str, str]:
    """判定出口 IP 的平台处境。返回 (verdict, evidence)。

    顺序：geo -> abuse -> quota -> auth -> 其它。
    任何状态码都先查 geo/abuse —— 修掉 v1 "400 直接判 auth_ok" 的盲区。
    """
    env_status, msg = _envelope(body)
    ml = msg.lower()

    hit = _hit(ml, GEO_MARKERS)
    if hit:
        return "geo_blocked", msg.strip()[:160]
    hit = _hit(ml, ABUSE_MARKERS)
    if hit:
        return "ip_flagged", msg.strip()[:160]
    if status == 429 or env_status == "RESOURCE_EXHAUSTED":
        return "quota_limited", (msg or env_status or f"HTTP {status}")[:160]
    hit = _hit(ml, AUTH_MARKERS)
    if hit:
        return "auth_ok", msg.strip()[:160]
    if status in (200, 204):
        return "ok", f"HTTP {status}"
    if status == 403:
        # 403 无任何证据：保守留证待判（不放行、也不拉黑）
        return "unknown", (msg or f"HTTP 403 {env_status}").strip()[:160]
    if status in (400, 401, 402) or env_status in (
            "UNAUTHENTICATED", "PERMISSION_DENIED", "INVALID_ARGUMENT",
            "NOT_FOUND", "FAILED_PRECONDITION"):
        # 到达后端并被业务拒绝、且无地区/滥用证据 -> IP 视为放行。
        # FAILED_PRECONDITION 无 location 文案时多为 OAuth/配置问题，与 IP 无关。
        return "auth_ok", (msg or f"HTTP {status}").strip()[:160]
    return "unknown", (msg or f"HTTP {status}").strip()[:160]


def _clean_env() -> None:
    """清掉系统代理环境变量 —— 否则请求会走本机 7890，得到的是代理的错误结果。"""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)


@dataclass
class ProbeResult:
    node: str
    ok: bool = False                 # = passed（兼容旧字段）
    status: int | None = None
    latency_ms: int | None = None
    blocked: bool = False            # = strong（实锤风控，兼容旧字段）
    error: str | None = None         # 仅传输层故障（timeout/连接失败）
    body_snippet: str | None = None
    verdict: str = "unknown"         # ok/auth_ok/geo_blocked/ip_flagged/quota_limited/unknown/unreachable
    evidence: str = ""               # 判定依据（命中的 message 原文片段）
    exit_ip: str | None = None       # 探活链路出口 IP（审计"测的是谁"）

    @property
    def passed(self) -> bool:
        """验证是否通过（可用于锁定节点）。unknown 不通过但也不拉黑。"""
        return self.verdict in PASSED_KINDS

    @property
    def strong(self) -> bool:
        """是否实锤风控（geo / 滥用封禁），可加重惩罚。"""
        return self.verdict in BLOCKED_KINDS

    def sync(self) -> "ProbeResult":
        """把分类结果同步到兼容字段 ok / blocked。"""
        self.ok = self.verdict in PASSED_KINDS
        self.blocked = self.verdict in BLOCKED_KINDS
        return self

    @property
    def label(self) -> str:
        """粗粒度兼容标签：ok / blocked / unreachable / unknown"""
        if self.status is None and self.error:
            return "unreachable"
        if self.verdict in BLOCKED_KINDS:
            return "blocked"
        if self.verdict in PASSED_KINDS:
            return "ok"
        return "unknown"

    @property
    def score(self) -> float:
        """探活归一化分 0~1，按判定分级。"""
        if self.verdict in BLOCKED_KINDS:
            return 0.0
        if self.verdict == "unknown":
            return 0.2
        if self.verdict == "quota_limited":
            return 0.5
        if self.verdict == "auth_ok":
            return 0.9
        # ok：按响应耗时给 0.6~1.0
        if self.latency_ms is None:
            return 0.7
        return round(max(0.6, min(1.0, (1000 - self.latency_ms) / 800.0)), 4)

    def to_dict(self) -> dict:
        return {
            "node": self.node, "ok": self.ok, "status": self.status,
            "latency_ms": self.latency_ms, "blocked": self.blocked,
            "label": self.label, "verdict": self.verdict,
            "evidence": self.evidence, "exit_ip": self.exit_ip,
            "passed": self.passed, "strong": self.strong,
            "error": self.error, "body_snippet": self.body_snippet,
        }


class Prober:
    def __init__(self, url: str, ok_status: list[int], blocked_status: list[int],
                 timeout: int = 8, attempts: int = 2,
                 api_key: str = "", key_url: str = "",
                 via: str = "", ip_echo_url: str = ""):
        self.url = url
        self.ok_status = set(ok_status)
        self.blocked_status = set(blocked_status)
        self.timeout = timeout
        self.attempts = attempts
        # 二级探针：真实业务探针（GEMINI_API_KEY 激活；未配置则停留在 edge 探针）
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.key_url = key_url or DEFAULT_KEY_URL
        # 三级探针：旁路 HTTP 监听（Mihomo listener，proxy 绑定到探活组）
        self.via = via
        # 出口 IP 回显（审计用，可关闭）
        self.ip_echo_url = ip_echo_url
        _clean_env()

    def _build_opener(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers = [urllib.request.HTTPSHandler(context=ctx)]
        if self.via:
            # 走旁路监听：流量被 Mihomo listener 按绑定组路由
            handlers.append(urllib.request.ProxyHandler(
                {"http": self.via, "https": self.via}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    # ---------- 基础请求 ----------
    def _request(self, node: str) -> ProbeResult:
        r = ProbeResult(node=node)
        opener = self._build_opener()
        if self.api_key:
            # key 探针：POST generateContent（1 token），x-goog-api-key 放 header
            req = urllib.request.Request(
                self.key_url,
                data=json.dumps(KEY_PROBE_BODY).encode(),
                headers={
                    "x-goog-api-key": self.api_key,
                    "Content-Type": "application/json",
                    "User-Agent": "curl/8.0",
                })
        else:
            # edge 探针：GET models（无 key）
            req = urllib.request.Request(self.url, headers={
                "User-Agent": "curl/8.0",
                "Accept": "application/json",
            })
        t0 = time.time()
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                r.status = resp.status
                r.latency_ms = int((time.time() - t0) * 1000)
                try:
                    r.body_snippet = resp.read(600).decode("utf-8", "ignore")
                except Exception:
                    pass
                r.verdict, r.evidence = classify_response(r.status, r.body_snippet)
        except urllib.error.HTTPError as e:
            r.status = e.code
            r.latency_ms = int((time.time() - t0) * 1000)
            try:
                r.body_snippet = e.read(600).decode("utf-8", "ignore")
            except Exception:
                pass
            r.verdict, r.evidence = classify_response(e.code, r.body_snippet)
        except socket.timeout:
            r.verdict, r.evidence = "unreachable", "timeout"
            r.error = "timeout"
        except urllib.error.URLError as e:
            reason = str(e.reason)[:80]
            r.verdict, r.evidence = "unreachable", reason
            r.error = f"urlerror: {reason}"
        except Exception as e:
            reason = f"{type(e).__name__}: {str(e)[:80]}"
            r.verdict, r.evidence = "unreachable", reason
            r.error = reason
        r.sync()
        return r

    def probe_exit_ip(self) -> str | None:
        """出口 IP 回显：与探活同链路，回答"刚才测的到底是哪个出口"。"""
        if not self.ip_echo_url:
            return None
        try:
            opener = self._build_opener()
            req = urllib.request.Request(
                self.ip_echo_url, headers={"User-Agent": "curl/8.0"})
            with opener.open(req, timeout=min(self.timeout, 5)) as resp:
                return (resp.read(64).decode("utf-8", "ignore").strip() or None)
        except Exception:
            return None

    # ---------- 对外 ----------
    def probe_current(self, node: str = "__current__") -> ProbeResult:
        """对当前生效出口探活，多次取最优。

        判定由 classify_response 完成（解析错误信封），
        不要按状态码覆写 —— 否则会把 400 地区风控误判成"IP 已放行"。
        """
        best: ProbeResult | None = None
        for _ in range(max(1, self.attempts)):
            r = self._request(node)
            if r.passed:
                return r
            if best is None:
                best = r
            elif best.status is None and r.status is not None:
                best = r
            elif best.strong and not r.strong:
                best = r          # 实锤风控 -> 未实锤的更可信
        return best  # type: ignore[return-value]

    def probe_tcp(self, host: str, port: int = 443,
                  node: str = "__current__") -> ProbeResult:
        """纯 TCP 连通性 —— 用于判断"节点通不通"，不涉及平台判据。"""
        r = ProbeResult(node=node)
        t0 = time.time()
        try:
            with socket.create_connection((host, port), timeout=self.timeout):
                r.latency_ms = int((time.time() - t0) * 1000)
                r.verdict, r.evidence = "ok", "tcp ok"
                r.ok = True
        except socket.timeout:
            r.verdict, r.evidence = "unreachable", "timeout"
            r.error = "timeout"
        except Exception as e:
            reason = f"{type(e).__name__}: {str(e)[:60]}"
            r.verdict, r.evidence = "unreachable", reason
            r.error = reason
        r.sync()
        return r
