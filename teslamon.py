#!/usr/bin/env python3
"""Tesla Model S 85/90 pack monitor.

Reads the battery-internal CAN (socketcan can0), decodes BMS broadcasts,
serves a live dashboard over HTTP/WebSocket on :8080.

Frame formats (validated empirically against this pack, 2026-07-02):
  0x6F2  byte0 = mux
         mux 0..23:  4 brick voltages, 14-bit LE bitstream in bytes 1..7,
                     0.000305 V/bit; bricks 4*mux .. 4*mux+3
         mux 24..31: 4 temperatures, same packing, 0.0122 degC/bit
  0x102  bytes 0-1 u16 LE pack voltage 0.01 V/bit
         bytes 2-3 s16 LE current 0.1 A/bit (sign convention unverified)
"""

import asyncio
import csv
import json
import os
import time

import can
from aiohttp import web, WSMsgType

CAN_IFACE = "can0"
HTTP_PORT = 8080
KEEPALIVE_PERIOD = 5.0
KEEPALIVE_ID = 0x555
BROADCAST_PERIOD = 1.0
LOG_PERIOD = 10.0
LOG_PATH = "/var/log/teslamon.csv"
CAN_RETRY_PERIOD = 3.0
MUX_STALE_S = 15.0
N_BRICKS = 96
N_TEMPS = 32
N_MUX = 32
BRICKS_PER_MODULE = 6

state = {
    "bricks": [None] * N_BRICKS,
    "temps": [None] * N_TEMPS,
    "brick_min": [None] * N_BRICKS,
    "brick_max": [None] * N_BRICKS,
    "mux_last_seen": [None] * N_MUX,
    "pack_v": None,
    "pack_a": None,
    "soc": None,
    "frame_count": 0,
    "last_rx": None,
    "started": time.time(),
    "can_status": "starting",
    "can_error": None,
}
clients = set()


def unpack14(data):
    bits = int.from_bytes(data[1:8], "little")
    return [(bits >> (14 * k)) & 0x3FFF for k in range(4)]


def handle_frame(msg):
    state["frame_count"] += 1
    state["last_rx"] = time.time()
    d = msg.data
    if msg.arbitration_id == 0x6F2 and len(d) == 8:
        mux = d[0]
        if mux < N_MUX:
            state["mux_last_seen"][mux] = time.time()
        if d[1:8] == b"\xff" * 7:
            if mux < 24:
                for k in range(4):
                    state["bricks"][4 * mux + k] = None
            elif mux < 32:
                for k in range(4):
                    state["temps"][4 * (mux - 24) + k] = None
            return
        vals = unpack14(d)
        if mux < 24:
            for k, raw in enumerate(vals):
                if raw in (0, 0x3FFF):
                    continue
                bi = 4 * mux + k
                v = round(raw * 0.000305, 4)
                state["bricks"][bi] = v
                lo = state["brick_min"][bi]
                hi = state["brick_max"][bi]
                if lo is None or v < lo:
                    state["brick_min"][bi] = v
                if hi is None or v > hi:
                    state["brick_max"][bi] = v
        elif mux < 32:
            for k, raw in enumerate(vals):
                ti = 4 * (mux - 24) + k
                if raw == 0x3FFF:
                    state["temps"][ti] = None
                    continue
                if raw & 0x2000:
                    raw -= 0x4000
                t = round(raw * 0.0122, 2)
                if t < -50 or t > 120:
                    state["temps"][ti] = None
                else:
                    state["temps"][ti] = t
    elif msg.arbitration_id == 0x302 and len(d) >= 2:
        raw = d[0] + ((d[1] & 0x03) << 8)
        if raw:
            state["soc"] = round(raw / 10.0, 1)
    elif msg.arbitration_id == 0x102 and len(d) >= 4:
        state["pack_v"] = round(int.from_bytes(d[0:2], "little") * 0.01, 2)
        state["pack_a"] = round(
            int.from_bytes(d[2:4], "little", signed=True) * 0.1, 1
        )


def effective_readings():
    """Return (bricks, temps) with stale muxes masked to None."""
    now = time.time()
    bricks = list(state["bricks"])
    temps = list(state["temps"])
    for m in range(N_MUX):
        last = state["mux_last_seen"][m]
        stale = last is None or (now - last) > MUX_STALE_S
        if not stale:
            continue
        if m < 24:
            for k in range(4):
                bricks[4 * m + k] = None
        else:
            for k in range(4):
                temps[4 * (m - 24) + k] = None
    return bricks, temps


def snapshot():
    bricks, temps = effective_readings()
    known = [v for v in bricks if v is not None]
    summary = {}
    if known:
        summary = {
            "min": min(known),
            "max": max(known),
            "delta": round(max(known) - min(known), 4),
            "sum": round(sum(known), 2),
        }
    return json.dumps(
        {
            "bricks": bricks,
            "brick_min": state["brick_min"],
            "brick_max": state["brick_max"],
            "temps": temps,
            "pack_v": state["pack_v"],
            "pack_a": state["pack_a"],
            "soc": state["soc"],
            "frame_count": state["frame_count"],
            "last_rx": state["last_rx"],
            "bricks_per_module": BRICKS_PER_MODULE,
            "summary": summary,
            "since": state["started"],
            "now": time.time(),
            "can_status": state["can_status"],
            "can_error": state["can_error"],
        }
    )


async def keepalive_loop(bus):
    msg = can.Message(
        arbitration_id=KEEPALIVE_ID, data=[0], is_extended_id=False
    )
    while True:
        try:
            bus.send(msg)
        except can.CanError:
            pass
        await asyncio.sleep(KEEPALIVE_PERIOD)


async def can_lifecycle():
    while True:
        try:
            bus = can.Bus(channel=CAN_IFACE, interface="socketcan")
        except OSError as e:
            state["can_status"] = "no_adapter"
            state["can_error"] = str(e)
            await asyncio.sleep(CAN_RETRY_PERIOD)
            continue
        state["can_status"] = "ok"
        state["can_error"] = None
        reader = can.AsyncBufferedReader()
        notifier = can.Notifier(
            bus, [reader], loop=asyncio.get_running_loop()
        )
        ka = asyncio.create_task(keepalive_loop(bus))
        try:
            async for msg in reader:
                handle_frame(msg)
        finally:
            ka.cancel()
            try:
                await ka
            except asyncio.CancelledError:
                pass
            notifier.stop()
            bus.shutdown()
        state["can_status"] = "no_adapter"
        await asyncio.sleep(CAN_RETRY_PERIOD)


async def csv_logger():
    new_file = not os.path.exists(LOG_PATH)
    f = open(LOG_PATH, "a", buffering=1)
    w = csv.writer(f)
    if new_file:
        header = ["ts_iso", "pack_v", "pack_a", "soc"]
        header += [f"b{i+1}" for i in range(N_BRICKS)]
        header += [f"t{i+1}" for i in range(N_TEMPS)]
        w.writerow(header)
    while True:
        await asyncio.sleep(LOG_PERIOD)
        bricks, temps = effective_readings()
        row = [
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            state["pack_v"],
            state["pack_a"],
            state["soc"],
        ]
        row += ["" if v is None else v for v in bricks]
        row += ["" if v is None else v for v in temps]
        w.writerow(row)


async def resetter(request):
    for i in range(N_BRICKS):
        state["brick_min"][i] = None
        state["brick_max"][i] = None
    return web.Response(text="min/max cleared\n")


async def broadcaster():
    while True:
        await asyncio.sleep(BROADCAST_PERIOD)
        if clients:
            data = snapshot()
            for ws in list(clients):
                try:
                    await ws.send_str(data)
                except ConnectionError:
                    clients.discard(ws)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    clients.add(ws)
    await ws.send_str(snapshot())
    try:
        async for msg in ws:
            if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        clients.discard(ws)
    return ws


async def state_handler(request):
    return web.Response(text=snapshot(), content_type="application/json")


async def csv_handler(request):
    if not os.path.exists(LOG_PATH):
        return web.Response(status=404, text="no log yet")
    filename = time.strftime("teslamon-%Y%m%d-%H%M%S.csv", time.localtime())
    return web.FileResponse(
        LOG_PATH,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def index_handler(request):
    return web.FileResponse("/opt/teslamon/index.html")


async def main():
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/state", state_handler)
    app.router.add_post("/reset", resetter)
    app.router.add_get("/log.csv", csv_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    await asyncio.gather(can_lifecycle(), broadcaster(), csv_logger())


if __name__ == "__main__":
    asyncio.run(main())
