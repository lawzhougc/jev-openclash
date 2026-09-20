# -*- coding: utf-8 -*-
"""
OpenClash / Mihomo External Controller API 客户端。

关键坑位（已实测）：
  * /proxies 只返回策略组，不含 provider 内的真实节点
  * 真实节点必须走 /providers/proxies/{provider}
  * /proxies/{节点}/delay 对 provider 内节点一律 404
  * 切换组用 PUT /proxies/{组名}，body {"name": "..."}
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Node:
    """一个可用的出口节点。"""
    name: str
    alive: bool = False
    delay: int | None = None
    history: list[dict] = field(default_factory=list)

    @property
    def delay_score(self) -> float:
        """延迟归一化到 0~1，越快越高。无数据返回 0。"""
        if self.delay is None:
            return 0.0
        # 50ms 以下满分，400ms 以上 0 分
        d = max(0.0, min(1.0, (400 - self.delay) / 350.0))
        return round(d, 4)


class OpenClashClient:
    def __init__(self, api: str, secret: str, timeout: int = 15):
        self.api = api.rstrip("/")
        self.secret = secret
        self.timeout = timeout
        # 局域网内的 OpenClash 必须直连 —— 走系统代理会得到 502
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
        )
        self._group_names_cache: set[str] | None = None
        self._groups_cache: dict = {}
        self._groups_cache_ts: float = 0.0

    def _refresh_groups(self, ttl: float = 5.0) -> dict:
        """策略组列表带短 TTL 缓存，避免一次决策里反复拉全量。"""
        import time as _t
        if self._groups_cache and (_t.time() - self._groups_cache_ts) < ttl:
            return self._groups_cache
        try:
            g = self.proxies()
        except Exception:
            return self._groups_cache
        self._groups_cache = g
        self._groups_cache_ts = _t.time()
        self._group_names_cache = set(g.keys())
        return g

    # ---------- 底层 ----------
    def _req(self, path: str, method: str = "GET", body: dict | None = None,
             timeout: int | None = None) -> Any:
        url = self.api + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self.secret,
            "Content-Type": "application/json",
        })
        with self._opener.open(req, timeout=timeout or self.timeout) as r:
            raw = r.read().decode("utf-8", "ignore")
        return json.loads(raw) if raw.strip() else {}

    # ---------- 读 ----------
    def version(self) -> dict:
        return self._req("/version")

    def proxies(self) -> dict:
        """全部策略组（不含 provider 内的真实节点）。"""
        return self._req("/proxies").get("proxies", {})

    def group(self, name: str) -> dict:
        return self._req("/proxies/" + urllib.parse.quote(name))

    def provider_nodes(self, provider: str) -> list[Node]:
        """provider 内的真实节点，带 alive 与 history.delay。"""
        data = self._req("/providers/proxies/" + urllib.parse.quote(provider))
        nodes: list[Node] = []
        for p in data.get("proxies", []):
            hist = p.get("history") or []
            delay = hist[-1].get("delay") if hist else None
            if delay is not None and delay <= 0:
                delay = None
            nodes.append(Node(
                name=p.get("name", ""),
                alive=bool(p.get("alive")),
                delay=delay,
                history=hist,
            ))
        return nodes

    def providers(self) -> list[str]:
        data = self._req("/providers/proxies")
        return list((data.get("providers") or {}).keys())

    def connections(self) -> dict:
        return self._req("/connections")

    # ---------- 写 ----------
    def select(self, group: str, node: str) -> bool:
        """
        把策略组切换到指定目标。

        **关键约束（实测）**：Mihomo 只允许切到该组的**直接成员**。
        跨组切换会返回 400 "proxy not exist"。
        因此本方法只做直接切换，跨组可达性由 find_path_to 处理。
        """
        try:
            self._req("/proxies/" + urllib.parse.quote(group),
                      method="PUT", body={"name": node})
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            raise RuntimeError(f"切换失败 HTTP {e.code}: {body[:200]}") from e

    def is_member(self, group: str, member: str) -> bool:
        g = self._refresh_groups().get(group)
        if not g:
            return False
        return member in (g.get("all") or [])

    def find_path_to(self, group: str, target_node: str,
                     max_depth: int = 3) -> list[tuple[str, str]] | None:
        """
        找一条从 group 出发、能最终让 target_node 生效的切换路径。

        由于 Mihomo 只能切直接成员，要落到一个真实节点，可能需要
        逐层下钻：  ♊ Gemini -> 🚀 默认代理 -> 🇯🇵 日本节点 -> JP-X5-1

        返回 [(组名, 要选中的成员), ...]，按顺序执行即可。
        找不到返回 None。
        """
        groups = self._refresh_groups()

        def search(cur: str, path: list[tuple[str, str]], depth: int):
            if depth > max_depth:
                return None
            g = groups.get(cur)
            if not g:
                return None
            members = g.get("all") or []

            # 情况 1：目标节点就是当前组的直接成员 —— 完美
            if target_node in members:
                return path + [(cur, target_node)]

            # 情况 2：目标节点不在成员里。尝试经由某个"组类型"成员下钻。
            # 优先选择包含目标节点的成员组，其次选能通往它的组。
            for m in members:
                sub = groups.get(m)
                if not sub:
                    continue
                sub_members = sub.get("all") or []
                # 该成员组直接含目标节点
                if target_node in sub_members:
                    return path + [(cur, m), (m, target_node)]
                # 更深一层
                if depth + 1 <= max_depth:
                    got = search(m, path + [(cur, m)], depth + 1)
                    if got:
                        return got
            return None

        return search(group, [], 0)

    def apply_path(self, path: list[tuple[str, str]]) -> list[str]:
        """按顺序执行切换路径，返回实际执行成功的步骤描述。"""
        done = []
        for grp, target in path:
            self.select(grp, target)
            done.append(f"{grp} -> {target}")
        return done

    def healthcheck(self, provider: str, timeout: int = 60) -> bool:
        """触发 provider 全量测速。"""
        try:
            self._req("/providers/proxies/" + urllib.parse.quote(provider) + "/healthcheck",
                      timeout=timeout)
            return True
        except Exception:
            return False

    # ---------- 组合 ----------
    def resolve_now(self, name: str, depth: int = 0) -> str:
        """
        把一个可能嵌套的"当前值"下钻到真实节点名。

        坑：策略组的 now 可能是另一个组名（如 '🔯 Gemini故转'、'♻️ Gemini自动'），
        直接拿它去比对节点分数会得到 0，导致引擎误判"当前节点很差"而反复切换。
        """
        if depth >= 4:
            return name
        groups = self._refresh_groups()
        g = groups.get(name)
        if not g:
            return name
        now = g.get("now")
        if not now or now == name:
            return name
        # Smart / URLTest 等自动组会返回内部值 "Smart - Select" 之类的伪节点，
        # 这不是真实节点，无法参与打分。这类情况保留组名本身，
        # 由 engine 走"当前值不在候选表 -> 视为未知"的分支处理。
        if now in ("Smart - Select", "Auto - Select", "URLTest - Select") or \
                now in (self._group_names_cache or set()):
            # 若 now 是另一个组，继续下钻
            if now in (self._group_names_cache or set()):
                return self.resolve_now(now, depth + 1)
            return name
        return now

    def resolve_candidates(self, candidate_groups: list[str],
                           provider: str) -> list[Node]:
        """
        从指定的若干策略组中，收集候选节点，并用 provider 的真实延迟数据补齐。

        策略组的成员可能是：真实节点名、或另一个策略组名（嵌套）。
        这里做一层展开：组 -> 成员；成员若在 provider 里则是节点，否则递归一层。
        """
        provider_map = {n.name: n for n in self.provider_nodes(provider)}
        all_groups = self._refresh_groups()

        picked: dict[str, Node] = {}

        def absorb(name: str, depth: int = 0):
            if name in provider_map:
                picked[name] = provider_map[name]
                return
            if depth >= 2:
                return
            g = all_groups.get(name)
            if g and g.get("all"):
                for m in g["all"]:
                    absorb(m, depth + 1)

        if candidate_groups:
            for gn in candidate_groups:
                absorb(gn)
        else:
            for n in provider_map.values():
                picked[n.name] = n

        return list(picked.values())
