# -*- coding: utf-8 -*-
"""
决策引擎：把三路信号合成一个选路决定，并执行"切换 -> 验证 -> 回滚"闭环。

三路信号：
    1. 延迟   —— 来自 provider 的 history.delay（快慢）
    2. 先验   —— 来自 Jev 的地区风险判断（平台认不认这个地区的 IP）
    3. 后验   —— 来自真实探活（切换后验证，事实推翻先验）

决策流程：
    候选节点 -> 打分排序 -> 选出最优
      -> 若当前节点已是最优（或优势不足），不做动作
      -> 否则切换 -> 探活验证
          -> 通过: 锁定，清该节点的失败计数
          -> 不通过: 立刻回滚到上一个可用节点 + 拉黑该节点，记录"事实推翻先验"

关键防抖：
    * cooldown    切换后 N 秒内不再切
    * hysteresis  新节点要领先当前节点一定比例才切
    * blacklist   连续失败达阈值进黑名单
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .collector import Node, OpenClashClient
from .jev import JevClient
from .prober import Prober, ProbeResult


# ---------- 地区识别 ----------
REGION_RULES = [
    ("japan",     ("JP-", "日本", "Japan")),
    ("singapore", ("SG-", "新加坡", "Singapore")),
    ("usa",       ("US-", "美国", "United States")),
    ("taiwan",    ("TW-", "台湾", "Taiwan")),
    ("korea",     ("KR-", "韩国", "Korea")),
    ("hongkong",  ("HK-", "香港", "Hong")),
]


def region_of(node_name: str) -> str:
    for region, keys in REGION_RULES:
        for k in keys:
            if k in node_name:
                return region
    if node_name.startswith("Fast") or node_name.startswith("Balancer"):
        return "auto"          # 中转/负载类，地区未知
    return "unknown"


@dataclass
class Candidate:
    name: str
    region: str
    alive: bool
    delay: int | None
    delay_score: float = 0.0
    prior: float = 0.5
    probe_score: float | None = None
    probed: bool = False            # probe_score 是否来自真实探活
    final: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name, "region": self.region, "alive": self.alive,
            "delay": self.delay, "delay_score": self.delay_score,
            "prior": self.prior, "probe_score": self.probe_score,
            "probed": self.probed,
            "final": self.final, "note": self.note,
        }


@dataclass
class Decision:
    ts: float
    target: str
    group: str
    action: str                 # "hold" | "switch" | "rollback"
    from_node: str | None
    to_node: str | None
    reason: str
    candidates: list[Candidate] = field(default_factory=list)
    probe: dict | None = None
    prior_table: dict = field(default_factory=dict)
    path: list = field(default_factory=list)

    def to_dict(self) -> dict:
        path_str = ""
        if self.path:
            # 形如:  ♊ Gemini → 🚀 默认代理 → 🔯 日本故转 → JP-X5-1
            segs = [p[0] for p in self.path]
            segs.append(self.path[-1][1])
            path_str = " → ".join(segs)
        return {
            "ts": self.ts,
            "time": time.strftime("%H:%M:%S", time.localtime(self.ts)),
            "target": self.target, "group": self.group,
            "action": self.action,
            "from_node": self.from_node, "to_node": self.to_node,
            "reason": self.reason,
            "probe": self.probe,
            "prior_table": self.prior_table,
            "path": path_str,
            "candidates": [c.to_dict() for c in self.candidates],
        }


class Engine:
    def __init__(self, cfg: dict, oc: OpenClashClient, jev: JevClient | None):
        self.cfg = cfg
        self.oc = oc
        self.jev = jev
        e = cfg.get("engine", {})
        self.w_lat = float(e.get("latency_weight", 0.40))
        self.w_probe = float(e.get("probe_weight", 0.45))
        self.w_prior = float(e.get("prior_weight", 0.15))
        self.cooldown = float(e.get("cooldown_sec", 90))
        self.hysteresis = float(e.get("hysteresis", 0.15))
        self.fail_threshold = int(e.get("fail_threshold", 3))
        self.blacklist_sec = float(e.get("blacklist_sec", 1800))
        self.interval = float(e.get("interval_sec", 120))
        self.probe_ttl = float(e.get("probe_cache_sec", 1800))

        # 运行时状态
        self.fail_count: dict[str, int] = {}
        self.blacklist: dict[str, float] = {}
        self.last_switch_ts: dict[str, float] = {}
        self.last_good: dict[str, str] = {}
        self.probe_cache: dict[str, tuple[float, ProbeResult]] = {}
        self._last_path: dict[str, list] = {}

    # ---------- 黑名单 ----------
    def _is_blacklisted(self, node: str) -> bool:
        ts = self.blacklist.get(node)
        if ts is None:
            return False
        if time.time() - ts > self.blacklist_sec:
            del self.blacklist[node]
            self.fail_count.pop(node, None)
            return False
        return True

    def _mark_fail(self, node: str):
        self.fail_count[node] = self.fail_count.get(node, 0) + 1
        if self.fail_count[node] >= self.fail_threshold:
            self.blacklist[node] = time.time()

    def _clear_fail(self, node: str):
        self.fail_count.pop(node, None)
        self.blacklist.pop(node, None)

    # ---------- 打分 ----------
    def score_candidates(self, nodes: list[Node], prior: dict) -> list[Candidate]:
        out: list[Candidate] = []
        now = time.time()
        for n in nodes:
            reg = region_of(n.name)
            p = prior.get(reg, 0.5)
            d = n.delay_score if n.alive else 0.0
            c = Candidate(name=n.name, region=reg, alive=n.alive,
                          delay=n.delay, delay_score=d, prior=p)
            hit = self.probe_cache.get(n.name)
            if hit and now - hit[0] <= self.probe_ttl:
                # 有真实探活记录：探活分进入公式，被风控/未通过直接压 0
                pres = hit[1]
                c.probed = True
                c.probe_score = pres.score
                c.final = self._combine(d, c.probe_score, p)
                if pres.blocked:
                    c.note = "探活被风控"
                elif not pres.ok:
                    c.note = "探活未通过"
            else:
                # 未探活的节点，probe_score 用先验替代（避免权重丢失）
                c.probe_score = p
                c.final = self._combine(d, p, p)
            if not n.alive:
                c.final = 0.0
                c.note = "节点不可达"
            if self._is_blacklisted(n.name):
                c.final = 0.0
                c.note = "黑名单中"
            out.append(c)
        out.sort(key=lambda c: c.final, reverse=True)
        return out

    def _combine(self, delay_score: float, probe_score: float,
                 prior: float) -> float:
        return round(self.w_lat * delay_score +
                     self.w_probe * probe_score +
                     self.w_prior * prior, 4)

    # ---------- 先验上下文构造 ----------
    # 实测经验：给 Jev 的地区描述越具体，判断区分度越高。
    # 只写"japan 出口"会得到模糊的 0.55；写明延迟区间、线路等级、
    # 存活状况，才会得到 0.83 这类有决策价值的值。
    REGION_CN = {
        "japan": "日本", "singapore": "新加坡", "usa": "美国",
        "taiwan": "中国台湾", "korea": "韩国", "hongkong": "中国香港",
    }
    # 线路等级说明：帮助 Jev 理解节点命名含义
    TIER_NOTE = {
        "Dedicated": "独享带宽线路（成本更高，稳定性好）",
        "X5": "标准中转线路",
        "X1": "基础线路",
        "D1": "直连线路",
        "IPv6": "走 IPv6 出口（部分海外服务对 IPv6 段风控更严）",
        "Netflix": "流媒体优化线路",
        "Balancer": "负载均衡线路",
        "Fast": "低延迟优选线路",
    }

    def _describe_region(self, region: str, nodes: list[Node]) -> dict:
        """把一个地区的节点聚合成一段给 Jev 读的描述。"""
        rs = [n for n in nodes if region_of(n.name) == region]
        alive = [n for n in rs if n.alive and n.delay]
        dead = [n for n in rs if not n.alive]
        cn = self.REGION_CN.get(region, region)

        parts = []
        if alive:
            ds = [n.delay for n in alive]
            # 识别线路等级
            tiers = []
            for key, note in self.TIER_NOTE.items():
                if any(key in n.name for n in rs):
                    tiers.append(f"{key}={note}")
            parts.append(f"{cn} ({region}): 共 {len(rs)} 个节点，"
                         f"{len(alive)} 个存活，实测延迟 {min(ds)}-{max(ds)}ms")
            if tiers:
                parts.append("线路类型: " + "；".join(tiers[:4]))
            parts.append("示例节点: " + ", ".join(n.name for n in alive[:3]))
        else:
            parts.append(f"{cn} ({region}): 共 {len(rs)} 个节点，"
                         f"当前全部不可达（alive=false）")
        if dead and alive:
            parts.append(f"其中 {len(dead)} 个节点掉线")

        return {"desc": "。".join(parts)}

    def _gather_regions(self, nodes: list[Node]) -> dict:
        seen = {}
        for n in nodes:
            r = region_of(n.name)
            if r in ("auto", "unknown") or r in seen:
                continue
            seen[r] = self._describe_region(r, nodes)
        return seen

    # ---------- 单轮决策 ----------
    def decide(self, target: dict) -> Decision:
        tname = target["name"]
        group = target["group"]
        provider = self.cfg["openclash"]["provider"]
        now = time.time()

        # 1. 候选池
        groups = target.get("candidate_groups") or []
        nodes = self.oc.resolve_candidates(groups, provider)

        # 2. 先验（Jev）
        prior = {}
        jev_cfg = self.cfg.get("jev", {})
        if self.jev and jev_cfg.get("enabled") and \
                jev_cfg.get("apply_to_decision", True):
            all_nodes = nodes
            try:
                all_nodes = self.oc.provider_nodes(provider)
            except Exception:
                pass
            regions = self._gather_regions(all_nodes)
            if regions:
                prior = self.jev.region_prior(
                    platform=tname,
                    service_desc=target.get("service_desc", ""),
                    regions=regions,
                    known_facts=target.get("known_facts", []),
                    cache_sec=int(jev_cfg.get("cache_sec", 3600)),
                )

        cands = self.score_candidates(nodes, prior)
        best = cands[0] if cands else None

        # 3. 当前生效节点
        #    注意：组的 now 可能是另一个组名（嵌套），必须下钻到真实节点，
        #    否则 cur_score 会算成 0，导致每轮都无意义地切换。
        try:
            raw_now = self.oc.group(group).get("now")
            cur = self.oc.resolve_now(raw_now or "")
        except Exception as ex:
            return Decision(now, tname, group, "hold", None, None,
                            f"读取组状态失败: {ex}", cands, prior_table=prior)

        if best is None or best.final <= 0:
            return Decision(now, tname, group, "hold", cur, None,
                            "无可用候选节点", cands, prior_table=prior)

        # 4. 冷却 / 滞后 判定
        last = self.last_switch_ts.get(tname, 0)
        if now - last < self.cooldown:
            left = int(self.cooldown - (now - last))
            return Decision(now, tname, group, "hold", cur, None,
                            f"冷却中，{left}s 后允许切换", cands,
                            prior_table=prior)

        if cur == best.name:
            return Decision(now, tname, group, "hold", cur, None,
                            f"当前节点已是最优（score={best.final}）", cands,
                            prior_table=prior)

        cur_cand = next((c for c in cands if c.name == cur), None)
        if cur_cand is None:
            # 当前生效节点不在候选池里（可能来自别的地区/别的组，
            # 或是个无法下钻的自动组）。这种情况不能当成 0 分判定，
            # 但也不该无限期保持不变 —— 用"最优候选"作为参照，要求明显优势。
            cur_score = best.final * (1 + self.hysteresis)
            reason_prefix = f"当前 {cur} 不在候选池"
        else:
            cur_score = cur_cand.final
            reason_prefix = ""
        if best.final < cur_score * (1 + self.hysteresis):
            return Decision(now, tname, group, "hold", cur, None,
                            f"{reason_prefix} 优势不足（{best.final} vs "
                            f"{round(cur_score, 4)}，"
                            f"需>={round(cur_score * (1 + self.hysteresis), 4)}）",
                            cands, prior_table=prior)

        # 5. 执行切换
        prev_good = self.last_good.get(tname) or cur
        reason = (f"{best.name} 优于当前 {cur}"
                  f"（{best.final} vs {cur_score}，延迟 {best.delay}ms，"
                  f"先验 {best.prior}）")

        probe_cfg = target.get("probe", {})
        dry = self.cfg.get("server", {}).get("dry_run")

        # 先解析切换路径（dry-run 也需要，用于展示引擎的意图）
        try:
            plan_path = self.oc.find_path_to(group, best.name)
        except Exception as ex:
            plan_path = None
            plan_err = str(ex)
        else:
            plan_err = ""
        if plan_path:
            reason += " | 路径 " + " → ".join(
                [p[0] for p in plan_path] + [plan_path[-1][1]])

        if dry:
            # DRY-RUN 也要探活 —— 否则无法验证判据是否正确。
            # 但不切换节点，因此探到的是"当前出口"而非"候选出口"。
            probe_res = None
            if probe_cfg.get("url"):
                pr = Prober(
                    url=probe_cfg["url"],
                    ok_status=probe_cfg.get("ok_status", [200, 400, 401, 403, 429]),
                    blocked_status=probe_cfg.get("blocked_status", [403]),
                    timeout=int(probe_cfg.get("timeout", 8)),
                    attempts=1,
                )
                pres = pr.probe_current("__dry_run_current__")
                probe_res = pres.to_dict()
            return Decision(now, tname, group, "switch", cur, best.name,
                            "[DRY-RUN] " + reason, cands, probe_res, prior,
                            plan_path or [])

        if not plan_path:
            self._mark_fail(best.name)
            return Decision(now, tname, group, "hold", cur, None,
                            f"无法从 {group} 到达 {best.name}（无可用切换路径"
                            f"{'：' + plan_err if plan_err else ''}）",
                            cands, prior_table=prior)

        try:
            self.oc.apply_path(plan_path)
            self._last_path[tname] = plan_path
        except Exception as ex:
            self._mark_fail(best.name)
            return Decision(now, tname, group, "hold", cur, None,
                            f"切换失败: {ex}", cands, prior_table=prior)

        self.last_switch_ts[tname] = now

        # 6. 探活验证
        probe_res = None
        if probe_cfg.get("url"):
            pr = Prober(
                url=probe_cfg["url"],
                ok_status=probe_cfg.get("ok_status", [200, 400, 401, 403, 429]),
                blocked_status=probe_cfg.get("blocked_status", [403]),
                timeout=int(probe_cfg.get("timeout", 8)),
                attempts=int(probe_cfg.get("attempts", 2)),
            )
            time.sleep(2)                       # 等 Mihomo 应用新出口
            pres = pr.probe_current(best.name)
            probe_res = pres.to_dict()
            self.probe_cache[best.name] = (time.time(), pres)

            if not pres.ok or pres.blocked:
                # 事实推翻先验 -> 回滚 + 拉黑
                self._mark_fail(best.name)
                back_path = None
                try:
                    back_path = self.oc.find_path_to(group, prev_good)
                    if back_path:
                        self.oc.apply_path(back_path)
                except Exception:
                    pass
                return Decision(
                    now, tname, group, "rollback", cur, prev_good,
                    f"{best.name} 探活失败（{pres.label}"
                    f"{' HTTP ' + str(pres.status) if pres.status else ''}"
                    f"{'/' + pres.verdict if pres.verdict else ''}），"
                    f"已回滚到 {prev_good}",
                    cands, probe_res, prior, back_path or [])

            self._clear_fail(best.name)
            self.last_good[tname] = best.name
            return Decision(now, tname, group, "switch", cur, best.name,
                            reason + f" | 探活通过 {pres.label}",
                            cands, probe_res, prior, plan_path)

        # 无探活配置
        self._clear_fail(best.name)
        self.last_good[tname] = best.name
        return Decision(now, tname, group, "switch", cur, best.name, reason,
                        cands, None, prior, plan_path)
