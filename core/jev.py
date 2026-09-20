# -*- coding: utf-8 -*-
"""
TypeSafe Jev 客户端。

定位（基于实测得出的结论）：
    Jev 是 System One 模型 —— 做"gut-check"级别的结构化判断，不做多因素推理。
    给它的题必须是**原子问题**。问它"为 12 个候选节点选最优"会得到一堆平的概率
    （实测 confidence 只有 0.38）；问它"日本出口访问 Gemini 被拒的概率低吗"则
    得到合理的 0.83。

    因此本项目里 Jev 的职责被限定为：**地区级先验风险判断**。
    逐节点的排序交给延迟数据，逐节点的验证交给真实探活。

API（实测确认）：
    POST https://api.typesafe.ai/v1/systemone
    Authorization: Bearer <key>
    body: {state, model, questions}
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass
class JevAnswer:
    qid: str
    type: str
    value: float               # noul -> 0~1; score -> 加权分; choice -> 概率
    confidence: float = 0.0
    probabilities: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


class JevClient:
    def __init__(self, api: str, key: str, model: str = "jev-latest",
                 timeout: int = 45):
        self.api = api
        self.key = key
        self.model = model
        self.timeout = timeout
        self._cache: dict[str, tuple[float, dict]] = {}

    # ---------- 底层 ----------
    def ask(self, state, questions: dict, retries: int = 4) -> dict | None:
        body = {"state": state, "model": self.model, "questions": questions}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        ctx = ssl.create_default_context()
        delay = 1.5
        for i in range(retries):
            try:
                req = urllib.request.Request(self.api, data=payload, headers={
                    "Authorization": "Bearer " + self.key,
                    "Content-Type": "application/json",
                    "User-Agent": "curl/8.0",
                })
                with urllib.request.urlopen(req, timeout=self.timeout,
                                            context=ctx) as r:
                    return json.loads(r.read().decode("utf-8", "ignore"))
            except urllib.error.HTTPError as e:
                code = e.code
                # 429 / 529 按文档要求指数退避
                if code in (429, 529):
                    time.sleep(delay)
                    delay *= 2
                    continue
                # 401/422 是配置问题，重试没意义
                return {"__error__": f"HTTP {code}",
                        "__body__": e.read().decode("utf-8", "ignore")[:300]}
            except Exception as e:
                time.sleep(delay)
                delay *= 2
        return None

    # ---------- 高层：地区先验 ----------
    def region_prior(self, platform: str, service_desc: str, regions: dict,
                     known_facts: list[str], cache_sec: int = 3600) -> dict:
        """
        判断各出口地区对该平台的"被放行概率"。

        regions: {"japan": {"desc": "...", "nodes": [...], "delay": (79,188)}, ...}
                 或简化的 {"japan": "描述字符串"}

        **关键经验（实测）**：Jev 的输出质量完全取决于 state 的信息密度。
        只写"japan 出口"会得到 0.55 这种模糊值；写上实测延迟、线路等级
        （Dedicated / X5）、地区可用性，才会得到 0.83 这样有区分度的结果。

        返回: {"japan": 0.83, ...}  取不到时返回 {}
        """
        cache_key = f"{platform}::" + ",".join(sorted(regions.keys()))
        now = time.time()
        if cache_key in self._cache:
            ts, val = self._cache[cache_key]
            if now - ts < cache_sec:
                return val

        lines = []
        for key, info in regions.items():
            if isinstance(info, str):
                lines.append(f"- {key}: {info}")
            else:
                lines.append(f"- {key}: {info.get('desc', '')}".rstrip())

        state = (
            f"目标服务: {platform}\n"
            f"{service_desc}\n"
            f"用户在中国大陆，通过机场（代理服务商）节点访问该服务。"
            f"出口 IP 的机房属性会影响平台的放行判断。\n\n"
            f"出口地区详情（含实测数据）:\n" + "\n".join(lines) +
            ("\n\n参考事实:\n" + "\n".join(f"- {f}" for f in known_facts)
             if known_facts else "")
        )

        questions = {}
        for key in regions:
            questions[f"ok_{key}"] = {
                "type": "noul",
                "instructions": f"{key} 出口 IP 访问 {platform} 时，"
                                f"被目标平台拒绝或被风控的概率低吗？",
            }

        resp = self.ask(state, questions)
        if not resp or "__error__" in (resp or {}):
            return {}
        out = {}
        for key in regions:
            a = resp.get("answers", {}).get(f"ok_{key}")
            if a and a.get("type") == "noul":
                out[key] = round(float(a.get("noul", 0.0)), 4)
        self._cache[cache_key] = (now, out)
        return out

    def last_usage(self) -> dict:
        return {}
