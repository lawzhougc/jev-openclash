from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import asyncio
import json
import random
from datetime import datetime

app = FastAPI()
active_connections = []

@app.websocket("/ws/decisions")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            await asyncio.sleep(1)
    except:
        if websocket in active_connections:
            active_connections.remove(websocket)

async def mock_jev_decisions():
    nodes = ["HK-Proxy-01", "JP-Relay-02", "SG-Direct-01"]
    while True:
        await asyncio.sleep(3)
        if not active_connections:
            continue
        selected = random.choice(nodes)
        record = {
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "latency_ms": random.randint(70, 450),
            "input_tokens": random.randint(200, 800),
            "primitive": "Choice",
            "selected": selected,
            "confidence": round(random.uniform(0.6, 0.99), 2),
            "probabilities": {
                node: round(random.uniform(0, 1), 2) for node in nodes
            },
            "trigger_reason": "状态轮询检查"
        }
        data = json.dumps(record)
        for conn in active_connections:
            try:
                await conn.send_text(data)
            except:
                pass

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(mock_jev_decisions())

# 挂载前端静态文件
app.mount("/assets", StaticFiles(directory="static/assets"), name="assets")

@app.get("/")
@app.get("/{catchall:path}")
def serve_react_app(catchall: str = ""):
    return FileResponse("static/index.html")
