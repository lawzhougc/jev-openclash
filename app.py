# -*- coding: utf-8 -*-
"""
SmartRoute —— OpenClash 智能选路服务

数据源：
    OpenClash External Controller API（延迟/存活/切组）
    TypeSafe Jev（地区级先验风险判断）
    真实 HTTP 探活（切换后验证，事实兜底）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent))

from core.collector import OpenClashClient
from core.engine import Engine, region_of
from core.jev import JevClient

ROOT = Path(__file__).parent
CFG_PATH = Path(os.environ.get("SMARTROUTE_CONFIG", ROOT / "config.yaml"))


def load_cfg() -> dict:
    with open(CFG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


CFG = load_cfg()

# Jev key: 环境变量优先，其次 config
JEV_KEY = os.environ.get("TYPESAFE_API_KEY") or CFG.get("jev", {}).get("key", "")

oc = OpenClashClient(
    api=CFG["openclash"]["api"],
    secret=CFG["openclash"]["secret"],
)
jev = None
if CFG.get("jev", {}).get("enabled") and JEV_KEY:
    jev = JevClient(
        api=CFG["jev"]["api"], key=JEV_KEY,
        model=CFG["jev"].get("model", "jev-latest"),
        timeout=int(CFG["jev"].get("timeout", 45)),
    )

engine = Engine(CFG, oc, jev)

app = FastAPI(title="SmartRoute")

# ---------------- 状态 ----------------
STATE = {
    "started": time.time(),
    "last_run": None,
    "last_error": None,
    "cycles": 0,
    "decisions": deque(maxlen=200),      # 全部决策历史
    "switches": deque(maxlen=100),       # 只保留真正动作的
    "node_snapshot": [],
    "group_snapshot": {},
    "jev_available": jev is not None,
}


class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def join(self, ws: WebSocket):
        await ws.accept()
        self.clients.add(ws)

    def leave(self, ws: WebSocket):
        self.clients.discard(ws)

    async def broadcast(self, payload: dict):
        if not self.clients:
            return
        msg = json.dumps(payload, ensure_ascii=False)
        dead = []
        for c in list(self.clients):
            try:
                await c.send_text(msg)
            except Exception:
                dead.append(c)
        for c in dead:
            self.clients.discard(c)


hub = Hub()


# ---------------- 循环 ----------------
async def run_cycle():
    """跑一轮：拉节点 -> 决策 -> 推送"""
    loop = asyncio.get_event_loop()

    provider = CFG["openclash"]["provider"]
    try:
        nodes = await loop.run_in_executor(
            None, lambda: oc.provider_nodes(provider))
    except Exception as ex:
        STATE["last_error"] = f"拉取节点失败: {ex}"
        await hub.broadcast({"type": "error", "message": STATE["last_error"]})
        return

    STATE["node_snapshot"] = [
        {"name": n.name, "alive": n.alive, "delay": n.delay,
         "region": region_of(n.name), "score": n.delay_score}
        for n in sorted(nodes, key=lambda x: (x.delay is None, x.delay or 9999))
    ]

    results = []
    for target in CFG.get("targets", []):
        try:
            d = await loop.run_in_executor(None, engine.decide, target)
            results.append(d)
            STATE["decisions"].append(d.to_dict())
            if d.action in ("switch", "rollback"):
                STATE["switches"].append(d.to_dict())
            await hub.broadcast({"type": "decision", "data": d.to_dict()})
        except Exception as ex:
            STATE["last_error"] = f"{target.get('name')} 决策异常: {ex}"
            await hub.broadcast({"type": "error",
                                 "message": STATE["last_error"]})

    # 组状态快照
    try:
        grp = await loop.run_in_executor(None, oc.proxies)
        watch = {t["group"] for t in CFG.get("targets", [])}
        STATE["group_snapshot"] = {
            k: {"type": v.get("type"), "now": v.get("now"),
                "members": v.get("all", [])[:40]}
            for k, v in grp.items() if k in watch
        }
        await hub.broadcast({"type": "groups", "data": STATE["group_snapshot"]})
    except Exception:
        pass

    await hub.broadcast({"type": "nodes", "data": STATE["node_snapshot"]})
    STATE["last_run"] = time.time()
    STATE["cycles"] += 1


async def scheduler():
    await asyncio.sleep(2)
    while True:
        try:
            await run_cycle()
        except Exception as ex:
            STATE["last_error"] = f"cycle: {ex}"
        await asyncio.sleep(engine.interval)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(scheduler())


# ---------------- HTTP ----------------
@app.get("/api/status")
def status():
    return {
        "ok": True,
        "uptime": int(time.time() - STATE["started"]),
        "cycles": STATE["cycles"],
        "last_run": STATE["last_run"],
        "last_error": STATE["last_error"],
        "interval": engine.interval,
        "cooldown": engine.cooldown,
        "dry_run": CFG.get("server", {}).get("dry_run", False),
        "jev_available": STATE["jev_available"],
        "blacklist": {k: int(time.time() - v) for k, v in engine.blacklist.items()},
        "fail_count": dict(engine.fail_count),
    }


@app.get("/api/nodes")
def nodes():
    return STATE["node_snapshot"]


@app.get("/api/decisions")
def decisions(limit: int = 50):
    return list(STATE["decisions"])[-limit:][::-1]


@app.get("/api/switches")
def switches(limit: int = 30):
    return list(STATE["switches"])[-limit:][::-1]


@app.get("/api/groups")
def groups():
    return STATE["group_snapshot"]


@app.get("/api/providers")
def providers():
    try:
        return {"providers": oc.providers()}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=500)


@app.post("/api/run")
async def run_now():
    await run_cycle()
    return {"ok": True, "cycles": STATE["cycles"]}


@app.get("/api/health")
def health():
    """连通性自检：分别验 OpenClash、Jev、探活目标。"""
    out = {"openclash": None, "jev": None, "probe": None}
    try:
        v = oc.version()
        out["openclash"] = {"ok": True, "version": v.get("version")}
    except Exception as ex:
        out["openclash"] = {"ok": False, "error": str(ex)}

    if jev:
        try:
            r = jev.ask("Connectivity test.", {
                "ping": {"type": "noul", "instructions": "Is this text non-empty?"}})
            ok = bool(r and "answers" in r)
            out["jev"] = {"ok": ok, "model": (r or {}).get("model")}
        except Exception as ex:
            out["jev"] = {"ok": False, "error": str(ex)}
    else:
        out["jev"] = {"ok": False, "error": "no key / disabled"}

    tgt = (CFG.get("targets") or [{}])[0]
    pc = (tgt.get("probe") or {})
    if pc.get("url"):
        from core.prober import Prober
        p = Prober(pc["url"], pc.get("ok_status", [200, 400, 401, 403, 429]),
                   pc.get("blocked_status", [403]),
                   int(pc.get("timeout", 8)), 1)
        r = p.probe_current("__direct__")
        out["probe"] = r.to_dict()
    return out


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await hub.join(ws)
    try:
        await ws.send_text(json.dumps({
            "type": "hello",
            "config": {"targets": [t["name"] for t in CFG.get("targets", [])],
                       "interval": engine.interval},
        }, ensure_ascii=False))
        if STATE["node_snapshot"]:
            await ws.send_text(json.dumps(
                {"type": "nodes", "data": STATE["node_snapshot"]},
                ensure_ascii=False))
        while True:
            await asyncio.sleep(20)
            await ws.send_text(json.dumps({"type": "ping"}, ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.leave(ws)


# ---------------- 静态 ----------------
STATIC = ROOT / "static"
if STATIC.exists():
    if (STATIC / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(STATIC / "assets")),
                  name="assets")

    @app.get("/")
    def index():
        return FileResponse(str(STATIC / "index.html"))
else:
    @app.get("/")
    def index_missing():
        return JSONResponse({"error": "static not built"}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    s = CFG.get("server", {})
    uvicorn.run(app, host=s.get("host", "0.0.0.0"),
                port=int(s.get("port", 8000)))
