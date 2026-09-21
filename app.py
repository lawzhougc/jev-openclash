# -*- coding: utf-8 -*-
"""
SmartRoute —— OpenClash 智能选路服务

数据源：
    OpenClash External Controller API（延迟/存活/切组）
    TypeSafe Jev（地区级先验风险判断）
    真实 HTTP 探活（切换后验证，事实兜底）

服务层职责：
    调度决策轮次（带互斥锁）、聚合状态、WebSocket 广播、
    决策落盘（data/decisions.jsonl，重启可恢复）、
    运维接口（暂停 / 强制切换 / 解除黑名单 / 触发测速）、
    可选访问令牌（server.token 或 SMARTROUTE_TOKEN）
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

import yaml
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent))

from core.collector import OpenClashClient
from core.engine import Engine, region_of
from core.jev import JevClient

ROOT = Path(__file__).parent
CFG_PATH = Path(os.environ.get("SMARTROUTE_CONFIG", ROOT / "config.yaml"))
DATA_DIR = Path(os.environ.get("SMARTROUTE_DATA", ROOT / "data"))


def load_cfg() -> dict:
    with open(CFG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


CFG = load_cfg()

# Jev key: 环境变量优先，其次 config
JEV_KEY = os.environ.get("TYPESAFE_API_KEY") or CFG.get("jev", {}).get("key", "")
# OpenClash secret: 环境变量优先，其次 config（建议用环境变量注入，避免入库）
OC_SECRET = os.environ.get("OPENCLASH_SECRET") or CFG["openclash"]["secret"]
# 面板访问令牌：空 = 不鉴权；设置后浏览器 Basic 认证（用户名任意，密码 = token）
AUTH_TOKEN = os.environ.get("SMARTROUTE_TOKEN") or CFG.get("server", {}).get("token", "") or ""

oc = OpenClashClient(
    api=CFG["openclash"]["api"],
    secret=OC_SECRET,
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

# ---------------- 访问控制（可选） ----------------
def _authed(request) -> bool:
    """HTTP / WebSocket 通用：token 空则放行；否则接受
    Bearer / Basic（密码 = token）/ ?token= 三种形式。"""
    if not AUTH_TOKEN:
        return True
    try:
        q = request.query_params.get("token", "")
        if q and q == AUTH_TOKEN:
            return True
        auth = request.headers.get("Authorization", "")
    except Exception:
        return False
    if auth.startswith("Bearer ") and auth[7:] == AUTH_TOKEN:
        return True
    if auth.startswith("Basic "):
        try:
            cred = base64.b64decode(auth[6:] + "==").decode("utf-8", "ignore")
            pw = cred.split(":", 1)[1] if ":" in cred else ""
            if pw == AUTH_TOKEN or cred == AUTH_TOKEN:
                return True
        except Exception:
            pass
    return False


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if _authed(request):
        return await call_next(request)
    return Response(
        status_code=401,
        content=json.dumps({"error": "unauthorized"}, ensure_ascii=False),
        media_type="application/json",
        headers={"WWW-Authenticate": 'Basic realm="SmartRoute"'},
    )


# ---------------- 状态 ----------------
STATE = {
    "started": time.time(),
    "last_run": None,
    "last_error": None,
    "cycles": 0,
    "decisions": deque(maxlen=200),      # 全部决策历史
    "switches": deque(maxlen=100),       # 只保留真正动作的
    "counts": {"switch": 0, "hold": 0, "rollback": 0},   # 生命周期计数（重启延续）
    "node_snapshot": [],
    "group_snapshot": {},
    "jev_available": jev is not None,
    "jev_ok": None,
    "oc_ok": None,
    "oc_version": None,
    "paused": False,
    "health_cache": None,
}

DECISIONS_FILE = DATA_DIR / "decisions.jsonl"


def _persist_decision(d: dict):
    """决策追加落盘。失败不影响主流程。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DECISIONS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_history():
    """启动时从 jsonl 恢复：计数用全量，展示用最近 200 条。"""
    if not DECISIONS_FILE.exists():
        return
    try:
        lines = DECISIONS_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    rows = []
    for ln in lines[-5000:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    for r in rows:
        a = r.get("action")
        if a in STATE["counts"]:
            STATE["counts"][a] += 1
    for r in rows[-200:]:
        STATE["decisions"].append(r)
        if r.get("action") in ("switch", "rollback"):
            STATE["switches"].append(r)


def _blacklist_detail() -> dict:
    now = time.time()
    out = {}
    for node, ts in engine.blacklist.items():
        elapsed = int(now - ts)
        out[node] = {
            "elapsed": elapsed,
            "remaining": max(0, int(engine.blacklist_sec - elapsed)),
            "fail_count": engine.fail_count.get(node, 0),
        }
    return out


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
cycle_lock = asyncio.Lock()      # 手动 /api/run 与调度器互斥，防竞态双切


# ---------------- 循环 ----------------
async def run_cycle():
    """跑一轮：拉节点 -> 决策 -> 落盘 -> 推送（全局互斥）"""
    async with cycle_lock:
        await _run_cycle_inner()


async def _run_cycle_inner():
    loop = asyncio.get_event_loop()

    provider = CFG["openclash"]["provider"]
    try:
        nodes = await loop.run_in_executor(
            None, lambda: oc.provider_nodes(provider))
        STATE["oc_ok"] = True
    except Exception as ex:
        STATE["oc_ok"] = False
        STATE["last_error"] = f"拉取节点失败: {ex}"
        await hub.broadcast({"type": "error", "message": STATE["last_error"]})
        return

    STATE["node_snapshot"] = [
        {"name": n.name, "alive": n.alive, "delay": n.delay,
         "region": region_of(n.name), "score": n.delay_score,
         "hist": [h.get("delay") for h in (n.history or [])[-24:]
                  if isinstance(h, dict) and h.get("delay")]}
        for n in sorted(nodes, key=lambda x: (x.delay is None, x.delay or 9999))
    ]

    results = []
    for target in CFG.get("targets", []):
        try:
            d = await loop.run_in_executor(None, engine.decide, target)
            results.append(d)
            dd = d.to_dict()
            STATE["decisions"].append(dd)
            STATE["counts"][d.action] = STATE["counts"].get(d.action, 0) + 1
            _persist_decision(dd)
            if d.action in ("switch", "rollback"):
                STATE["switches"].append(dd)
            # Jev 可用性：有先验输出即认为最近一次成功
            if jev:
                STATE["jev_ok"] = bool(d.prior_table)
            await hub.broadcast({"type": "decision", "data": dd})
        except Exception as ex:
            STATE["last_error"] = f"{target.get('name')} 决策异常: {ex}"
            await hub.broadcast({"type": "error",
                                 "message": STATE["last_error"]})

    # 组状态快照（含 resolve_now 下钻后的真实节点）
    try:
        grp = await loop.run_in_executor(None, oc.proxies)
        watch = {t["group"] for t in CFG.get("targets", [])}
        STATE["group_snapshot"] = {
            k: {"type": v.get("type"), "now": v.get("now"),
                "resolved": oc.resolve_now(v.get("now") or ""),
                "members": v.get("all", [])[:40]}
            for k, v in grp.items() if k in watch
        }
        await hub.broadcast({"type": "groups", "data": STATE["group_snapshot"]})
    except Exception:
        pass

    # 巡游探检：主动体检候选节点是否被平台风控（engine.patrol 内部节流）
    try:
        patrolled = False
        for target in CFG.get("targets", []):
            events = await loop.run_in_executor(None, engine.patrol, target)
            if events:
                patrolled = True
        if patrolled:
            await hub.broadcast({"type": "risk", "data": engine.node_risk})
    except Exception:
        pass

    await hub.broadcast({"type": "nodes", "data": STATE["node_snapshot"]})
    STATE["last_run"] = time.time()
    STATE["cycles"] += 1


async def scheduler():
    await asyncio.sleep(2)
    while True:
        try:
            if not STATE["paused"]:
                await run_cycle()
        except Exception as ex:
            STATE["last_error"] = f"cycle: {ex}"
        await asyncio.sleep(engine.interval)


@app.on_event("startup")
async def _startup():
    _load_history()
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
        "paused": STATE["paused"],
        "jev_available": STATE["jev_available"],
        "jev_ok": STATE["jev_ok"],
        "oc_ok": STATE["oc_ok"],
        "oc_version": STATE["oc_version"],
        "weights": {"latency": engine.w_lat, "probe": engine.w_probe,
                    "prior": engine.w_prior},
        "counts": dict(STATE["counts"]),
        "targets": [t["name"] for t in CFG.get("targets", [])],
        "blacklist": _blacklist_detail(),
        "fail_count": dict(engine.fail_count),
        "token_required": bool(AUTH_TOKEN),
        # ---- Gemini 风控判定（v2） ----
        "criterion_suspect": engine.criterion_suspect,
        "node_risk": engine.node_risk,
        "patrol": {"enabled": engine.patrol_enabled,
                   "mode": engine.patrol_mode,
                   "top_k": engine.patrol_top_k,
                   "interval": engine.patrol_interval},
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


@app.get("/api/risks")
def risks():
    """Gemini 风控档案：每个节点的判定、证据原文、出口 IP、来源。"""
    return {"criterion_suspect": engine.criterion_suspect,
            "node_risk": engine.node_risk}


@app.get("/api/providers")
def providers():
    try:
        return {"providers": oc.providers()}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=500)


@app.post("/api/run")
async def run_now():
    if cycle_lock.locked():
        return JSONResponse(
            {"ok": False, "error": "已有决策轮次在执行中"}, status_code=409)
    await run_cycle()
    return {"ok": True, "cycles": STATE["cycles"]}


@app.post("/api/pause")
async def pause(body: dict):
    """{"on": true} 暂停自动决策；{"on": false} 恢复。手动 /api/run 不受影响。"""
    STATE["paused"] = bool(body.get("on", True))
    return {"ok": True, "paused": STATE["paused"]}


@app.post("/api/unblacklist")
async def unblacklist(body: dict):
    """{"node": "xxx"} 解除单个；{"node": "*"} 全部解除。"""
    node = body.get("node") or "*"
    if node == "*":
        for n in list(engine.blacklist):
            engine._clear_fail(n)
    else:
        engine._clear_fail(node)
    return {"ok": True, "blacklist": _blacklist_detail()}


@app.post("/api/healthcheck")
async def healthcheck():
    """触发 provider 全量测速（约 1 分钟）。"""
    provider = CFG["openclash"]["provider"]
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(
        None, lambda: oc.healthcheck(provider, 60))
    return {"ok": ok}


@app.post("/api/patrol")
async def patrol_now():
    """立即触发一轮巡游探检（忽略 interval 节流），逐个验证候选节点。"""
    loop = asyncio.get_event_loop()
    total = []
    for target in CFG.get("targets", []):
        engine.last_patrol_ts.pop(target["name"], None)   # 忽略节流
        try:
            events = await loop.run_in_executor(None, engine.patrol, target)
            total.extend(events)
        except Exception as ex:
            total.append({"error": str(ex)[:120]})
    await hub.broadcast({"type": "risk", "data": engine.node_risk})
    return {"ok": True, "events": total, "node_risk": engine.node_risk}


@app.post("/api/force")
async def force(body: dict):
    """强制切换：{"target": "...", "node": "..."}。
    dry-run 下只预演可达路径不执行；真实模式按路径逐级切换。
    不走引擎验证闭环，请谨慎使用。"""
    tname = body.get("target")
    node = body.get("node")
    target = next((t for t in CFG.get("targets", [])
                   if t["name"] == tname), None)
    if not target or not node:
        return JSONResponse(
            {"ok": False, "error": "缺少 target 或 node"}, status_code=400)
    group = target["group"]
    loop = asyncio.get_event_loop()
    try:
        plan = await loop.run_in_executor(
            None, lambda: oc.find_path_to(group, node))
    except Exception as ex:
        return JSONResponse(
            {"ok": False, "error": f"解析路径失败: {ex}"}, status_code=500)
    if not plan:
        return JSONResponse(
            {"ok": False, "error": f"无可达路径: {group} -> {node}"},
            status_code=400)
    path_str = " → ".join([p[0] for p in plan] + [plan[-1][1]])
    dry = CFG.get("server", {}).get("dry_run")
    if dry:
        return {"ok": True, "dry_run": True, "plan": path_str,
                "steps": [list(p) for p in plan]}
    try:
        await loop.run_in_executor(None, oc.apply_path, plan)
    except Exception as ex:
        return JSONResponse(
            {"ok": False, "error": f"切换失败: {ex}", "plan": path_str},
            status_code=500)
    return {"ok": True, "dry_run": False, "plan": path_str,
            "steps": [list(p) for p in plan]}


@app.get("/api/health")
def health(force: int = 0):
    """连通性自检：分别验 OpenClash、Jev、探活目标。结果缓存 60s，?force=1 强制重测。"""
    now = time.time()
    if not force and STATE["health_cache"] and now - STATE["health_cache"][0] < 60:
        return STATE["health_cache"][1]

    out = {"openclash": None, "jev": None, "probe": None, "ts": now}
    try:
        v = oc.version()
        STATE["oc_ok"] = True
        STATE["oc_version"] = v.get("version")
        out["openclash"] = {"ok": True, "version": v.get("version")}
    except Exception as ex:
        STATE["oc_ok"] = False
        out["openclash"] = {"ok": False, "error": str(ex)}

    if jev:
        try:
            r = jev.ask("Connectivity test.", {
                "ping": {"type": "noul", "instructions": "Is this text non-empty?"}})
            ok = bool(r and "answers" in r)
            STATE["jev_ok"] = ok
            out["jev"] = {"ok": ok, "model": (r or {}).get("model")}
        except Exception as ex:
            STATE["jev_ok"] = False
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

    STATE["health_cache"] = (now, out)
    return out


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    if not _authed(ws):
        await ws.close(code=4401)
        return
    await hub.join(ws)
    try:
        await ws.send_text(json.dumps({
            "type": "hello",
            "config": {"targets": [t["name"] for t in CFG.get("targets", [])],
                       "interval": engine.interval,
                       "weights": {"latency": engine.w_lat,
                                   "probe": engine.w_probe,
                                   "prior": engine.w_prior}},
        }, ensure_ascii=False))
        if STATE["node_snapshot"]:
            await ws.send_text(json.dumps(
                {"type": "nodes", "data": STATE["node_snapshot"]},
                ensure_ascii=False))
        if STATE["group_snapshot"]:
            await ws.send_text(json.dumps(
                {"type": "groups", "data": STATE["group_snapshot"]},
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
