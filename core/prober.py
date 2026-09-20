# -*- coding: utf-8 -*-
"""
探活层：对"当前生效出口"发真实 HTTP 请求，判断平台是否认这个出口 IP。

设计约束（重要）：
    本项目不改 OpenClash 配置（代理商节点会变，手改必失效），
    也不做"逐节点探活"（那需要把生产策略组反复切来切去，会打断用户流量）。

    因此探活只做一件事：**验证当前出口**。
    逐节点筛选由 provider 的延迟数据 + Jev 的地区先验完成，
    切换后立即探活验证，不通过则回滚 —— 见 engine.py 的 verify-and-rollback。

判据（重要：必须解析响应体，不能只看状态码）：
    Google API 的 403 有两种完全不同含义，只看状态码会误判：
      a) PERMISSION_DENIED + "unregistered callers" / "API key"
         -> IP 已放行，只是没给 key          => 可用
      b) PERMISSION_DENIED + "location" / "country" / "region" / "territory"
         -> IP 被地区限制                      => 被风控
    其他：
      200            -> 完全可用
      400 / 401      -> IP 放行，参数/鉴权不对 => 可用
      429            -> IP 正常但配额受限      => 可用（记标记）
      超时 / 连接重置  -> 节点不通              => 不可用
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

# 判定"地区受限"的关键词（出现在响应体里才算真被风控）
GEO_BLOCK_MARKERS = (
    "location", "country", "region", "territory", "not available",
    "unsupported", "restricted", "region_blocked", "not supported",
)
# 判定"只是缺 key / 参数问题"的关键词
AUTH_MARKERS = (
    "api key", "api_key", "apikey", "unregistered", "credential",
    "authentication", "unauthorized", "identity", "oauth",
)


@dataclass
class ProbeResult:
    node: str
    ok: bool = False
    status: int | None = None
    latency_ms: int | None = None
    blocked: bool = False
    error: str | None = None
    body_snippet: str | None = None
    verdict: str = ""          # auth_ok / geo_blocked / ok / unreachable

    @property
    def label(self) -> str:
        if self.error:
            return "unreachable"
        if self.blocked:
            return "blocked"
        return "ok" if self.ok else "unknown"

    @property
    def score(self) -> float:
        """探活归一化分 0~1。"""
        if not self.ok or self.blocked:
            return 0.0
        if self.latency_ms is None:
            return 0.7
        return round(max(0.6, min(1.0, (1000 - self.latency_ms) / 800.0)), 4)

    def to_dict(self) -> dict:
        return {
            "node": self.node, "ok": self.ok, "status": self.status,
            "latency_ms": self.latency_ms, "blocked": self.blocked,
            "label": self.label, "verdict": self.verdict, "error": self.error,
            "body_snippet": self.body_snippet,
        }


def classify_body(status: int | None, body: str | None) -> tuple[str, bool]:
    """
    解析响应体，判断这是"IP 放行但缺鉴权"还是"IP 被地区风控"。

    返回 (verdict, blocked)
    """
    body_l = (body or "").lower()

    if status in (200, 204):
        return "ok", False
    if status in (400, 401):
        return "auth_ok", False          # 4xx 参数/鉴权错，说明 IP 通了
    if status == 429:
        return "auth_ok", False          # 配额限制，IP 正常
    if status == 403:
        # 关键区分：地区词优先
        if any(m in body_l for m in GEO_BLOCK_MARKERS):
            return "geo_blocked", True
        if any(m in body_l for m in AUTH_MARKERS):
            return "auth_ok", False
        # 403 但响应体无法判别 —— 保守当作被风控，但要留痕
        return "unknown_403", True
    if status and 400 <= status < 500:
        return "auth_ok", False
    return "unknown", False


def _clean_env() -> None:
    """清掉系统代理环境变量 —— 否则请求会走本机 7890，得到的是代理的错误结果。"""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)


class Prober:
    def __init__(self, url: str, ok_status: list[int], blocked_status: list[int],
                 timeout: int = 8, attempts: int = 2):
        self.url = url
        self.ok_status = set(ok_status)
        self.blocked_status = set(blocked_status)
        self.timeout = timeout
        self.attempts = attempts
        _clean_env()

    # ---------- 基础请求 ----------
    def _request(self, node: str) -> ProbeResult:
        r = ProbeResult(node=node)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),          # 全部直连，走系统路由/Mihomo TUN
            urllib.request.HTTPSHandler(context=ctx),
        )
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
                verdict, blocked = classify_body(resp.status, r.body_snippet)
                r.verdict = verdict
                r.blocked = blocked
                r.ok = not blocked
        except urllib.error.HTTPError as e:
            r.status = e.code
            r.latency_ms = int((time.time() - t0) * 1000)
            try:
                r.body_snippet = e.read(600).decode("utf-8", "ignore")
            except Exception:
                pass
            # 4xx 说明 TCP/TLS 通了、能收到平台响应 -> 出口 IP 被放行。
            # 但 403 必须解析响应体才能区分"缺 key"与"地区受限"。
            verdict, blocked = classify_body(e.code, r.body_snippet)
            r.verdict = verdict
            r.blocked = blocked
            r.ok = (400 <= e.code < 500) and not blocked
            if not r.ok:
                r.error = f"HTTP {e.code} ({verdict})"
        except socket.timeout:
            r.error = "timeout"
        except urllib.error.URLError as e:
            r.error = f"urlerror: {str(e.reason)[:80]}"
        except Exception as e:
            r.error = f"{type(e).__name__}: {str(e)[:80]}"
        return r

    # ---------- 对外 ----------
    def probe_current(self, node: str = "__current__") -> ProbeResult:
        """对当前生效出口探活，多次取最优。

        blocked 判定已由 _request 内部的 classify_body 完成（解析响应体），
        这里不要再按状态码覆写 —— 否则会把"缺 key 的 403"误判成风控。
        """
        best: ProbeResult | None = None
        for _ in range(max(1, self.attempts)):
            r = self._request(node)
            if r.ok and not r.blocked:
                return r
            if best is None:
                best = r
            elif best.status is None and r.status is not None:
                best = r
            elif best.blocked and not r.blocked:
                best = r
        return best  # type: ignore[return-value]

    def probe_tcp(self, host: str, port: int = 443,
                  node: str = "__current__") -> ProbeResult:
        """纯 TCP 连通性 —— 用于判断"节点通不通"，不涉及平台判据。"""
        r = ProbeResult(node=node)
        t0 = time.time()
        try:
            with socket.create_connection((host, port), timeout=self.timeout):
                r.latency_ms = int((time.time() - t0) * 1000)
                r.ok = True
        except socket.timeout:
            r.error = "timeout"
        except Exception as e:
            r.error = f"{type(e).__name__}: {str(e)[:60]}"
        return r
