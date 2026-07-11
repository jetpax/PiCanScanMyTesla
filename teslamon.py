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
MUX_DATA_STALE_S = 45.0
N_BRICKS = 96
N_TEMPS = 32
N_MUX = 32
BRICKS_PER_MODULE = 6

# UDS diagnostics (BMS on 0x602 req / 0x612 rsp). Security seed is static on
# this BMS; KEY is the constant that unlocks it (from an AIDBOX capture).
UDS_REQ_ID = 0x602
UDS_RSP_ID = 0x612
UDS_KEY = bytes([0x35, 0x34, 0x37, 0x36, 0x31, 0x30, 0x33, 0x32,
                 0x3D, 0x3C, 0x3F, 0x3E, 0x39, 0x38, 0x3B, 0x3A])
# Fault-clear routine IDs to enumerate, and tentative names from the AIDBOX
# request/response trace (main trace pairs; treat names as approximate).
FAULT_ROUTINES = list(range(0x0401, 0x0411))
FAULT_NAMES = {0x0401: "~u025", 0x040A: "~F026", 0x040C: "~F152",
               0x040D: "~F107"}

state = {
    "bricks": [None] * N_BRICKS,
    "temps": [None] * N_TEMPS,
    "brick_min": [None] * N_BRICKS,
    "brick_max": [None] * N_BRICKS,
    "mux_data_seen": [None] * N_MUX,
    "pack_v": None,
    "pack_a": None,
    "soc": None,
    "frame_count": 0,
    "last_rx": None,
    "started": time.time(),
    "can_status": "starting",
    "can_error": None,
    "health": {},
}
brick_last_event = [None] * N_BRICKS
clients = set()
EXCURSION_MV = 15.0
BAL_ACTIVE_S = 90.0


def unpack14(data):
    bits = int.from_bytes(data[1:8], "little")
    return [(bits >> (14 * k)) & 0x3FFF for k in range(4)]


def handle_frame(msg):
    state["frame_count"] += 1
    state["last_rx"] = time.time()
    d = msg.data
    if msg.arbitration_id == 0x6F2 and len(d) == 8:
        mux = d[0]
        if mux >= N_MUX:
            return
        # The BMS periodically retransmits each 8-mux block as all-FF (a
        # rotating invalidate marker, one block every ~10 s). This is normal
        # cadence, not a fault: ignore those frames entirely. A genuinely
        # unreachable BMB produces NO data frames at all, which surfaces as
        # data-staleness in effective_readings().
        if d[1:8] == b"\xff" * 7:
            return
        vals = unpack14(d)
        got_data = False
        if mux < 24:
            for k, raw in enumerate(vals):
                if raw in (0, 0x3FFF):
                    continue
                bi = 4 * mux + k
                v = round(raw * 0.000305, 4)
                prev = state["bricks"][bi]
                if prev is not None and abs(v - prev) * 1000 > EXCURSION_MV:
                    brick_last_event[bi] = time.time()
                state["bricks"][bi] = v
                got_data = True
                lo = state["brick_min"][bi]
                hi = state["brick_max"][bi]
                if lo is None or v < lo:
                    state["brick_min"][bi] = v
                if hi is None or v > hi:
                    state["brick_max"][bi] = v
        else:
            for k, raw in enumerate(vals):
                if raw == 0x3FFF:
                    continue
                if raw & 0x2000:
                    raw -= 0x4000
                t = round(raw * 0.0122, 2)
                if t < -50 or t > 120:
                    continue
                state["temps"][4 * (mux - 24) + k] = t
                got_data = True
        if got_data:
            state["mux_data_seen"][mux] = time.time()
    elif msg.arbitration_id == 0x302 and len(d) >= 2:
        raw = d[0] + ((d[1] & 0x03) << 8)
        if raw:
            state["soc"] = round(raw / 10.0, 1)
    elif msg.arbitration_id == 0x102 and len(d) >= 4:
        state["pack_v"] = round(int.from_bytes(d[0:2], "little") * 0.01, 2)
        state["pack_a"] = round(
            int.from_bytes(d[2:4], "little", signed=True) * 0.1, 1
        )
    elif msg.arbitration_id == 0x212 and len(d) >= 4:
        h = state["health"]
        h["hvil_on"] = bool((d[1] >> 3) & 1)
        h["bms_state"] = (d[2] >> 4) & 0x0F
        h["contactor"] = d[2] & 0x0F
        h["iso_kohm"] = None if d[3] == 0xFF else d[3] * 20
    elif msg.arbitration_id == 0x202 and len(d) >= 8:
        h = state["health"]
        h["vmin_limit"] = round(int.from_bytes(d[0:2], "little") * 0.01, 1)
        h["vmax_limit"] = round(int.from_bytes(d[2:4], "little") * 0.01, 1)
        h["max_chg_a"] = round(
            (int.from_bytes(d[4:6], "little") & 0x3FFF) * 0.1, 1
        )
    elif msg.arbitration_id == 0x382 and len(d) >= 8:
        bits = int.from_bytes(d, "little")
        raws = [(bits >> (10 * k)) & 0x3FF for k in range(5)]
        h = state["health"]
        full = raws[0] if raws[0] < 1000 else None
        h["nom_full_kwh"] = round(full * 0.1, 1) if full else None

        def rem(raw):
            if raw >= 1000 or (full and raw > full):
                return None
            return round(raw * 0.1, 1)

        h["nom_remaining_kwh"] = rem(raws[1])
        h["ideal_remaining_kwh"] = rem(raws[3])
    elif msg.arbitration_id == 0x3D2 and len(d) >= 8:
        h = state["health"]
        h["lifetime_charge_kwh"] = round(
            int.from_bytes(d[0:4], "little") / 1000.0, 1
        )
        h["lifetime_discharge_kwh"] = round(
            int.from_bytes(d[4:8], "little") / 1000.0, 1
        )
    elif msg.arbitration_id == 0x562 and len(d) >= 4:
        state["health"]["odometer_mi"] = round(
            int.from_bytes(d[0:4], "little") * 0.001
        )
    elif msg.arbitration_id == 0x332 and len(d) >= 8:
        mx = round((((d[1] & 0x0F) << 8) | d[0]) * 0.002, 3)
        mn = round(int.from_bytes(d[4:6], "little") * 0.002, 3)
        if 2.0 < mn <= mx < 4.5:
            h = state["health"]
            h["bms_brick_max"] = mx
            h["bms_brick_min"] = mn
            h["bms_temp_max"] = round(d[3] * 0.5 - 40, 1)
            h["bms_temp_min"] = round(d[7] * 0.5 - 40, 1)


def effective_readings():
    """Return (bricks, temps) with a mux masked to None once it has delivered
    no real data for MUX_DATA_STALE_S seconds (several full broadcast cycles).
    That is the signature of an unreachable BMB / broken daisy chain; the
    BMS's routine all-FF invalidate bursts never trigger it."""
    now = time.time()
    bricks = list(state["bricks"])
    temps = list(state["temps"])
    for m in range(N_MUX):
        last = state["mux_data_seen"][m]
        if last is not None and (now - last) <= MUX_DATA_STALE_S:
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
            "health": state["health"],
            "bal_active": [
                i + 1
                for i, ts in enumerate(brick_last_event)
                if ts is not None and time.time() - ts < BAL_ACTIVE_S
            ],
            "dtc": state.get("dtc"),
        }
    )


def uds_read_faults():
    """Blocking: open a dedicated socket, unlock security, and READ each
    fault routine's result (0x31 03, requestRoutineResults). Never sends
    0x31 01 (startRoutine / clear). Returns a result dict."""
    bus = can.Bus(channel=CAN_IFACE, interface="socketcan")

    def raw(data):
        bus.send(can.Message(arbitration_id=UDS_REQ_ID,
                             data=data.ljust(8, b"\x00"), is_extended_id=False))

    def send(payload):
        if len(payload) <= 7:
            raw(bytes([len(payload)]) + payload)
            return
        total = len(payload)
        raw(bytes([0x10 | (total >> 8), total & 0xFF]) + payload[:6])
        dl = time.time() + 1.0
        while time.time() < dl:
            m = bus.recv(timeout=dl - time.time())
            if m and m.arbitration_id == UDS_RSP_ID and (m.data[0] >> 4) == 3:
                break
        idx, sn = 6, 1
        while idx < total:
            raw(bytes([0x20 | (sn & 0x0F)]) + payload[idx:idx + 7])
            idx += 7
            sn += 1
            time.sleep(0.005)

    def recv(timeout=1.2):
        dl = time.time() + timeout
        buf = b""
        exp = None
        while time.time() < dl:
            m = bus.recv(timeout=max(0.01, dl - time.time()))
            if not m or m.arbitration_id != UDS_RSP_ID:
                continue
            d = m.data
            pci = d[0] >> 4
            if pci == 0:
                return d[1:1 + (d[0] & 0x0F)]
            if pci == 1:
                exp = ((d[0] & 0x0F) << 8) | d[1]
                buf = bytes(d[2:8])
                raw(bytes([0x30, 0, 0]))
            elif pci == 2:
                buf += bytes(d[1:8])
                if exp and len(buf) >= exp:
                    return buf[:exp]
        return buf or None

    def req(payload, t=1.2):
        send(payload)
        return recv(t)

    try:
        req(bytes([0x10, 0x03]))
        req(bytes([0x27, 0x05]))
        unlock = req(bytes([0x27, 0x06]) + UDS_KEY)
        if not unlock or unlock[0] != 0x67:
            return {"ok": False, "error": "security access denied",
                    "ts": time.time()}
        faults = []
        for rid in FAULT_ROUTINES:
            r = req(bytes([0x31, 0x03, rid >> 8, rid & 0xFF]))
            if r is None or r[0] == 0x7F or len(r) < 5:
                continue  # invalid/absent routine
            status = r[4]
            faults.append({
                "routine": rid,
                "name": FAULT_NAMES.get(rid, ""),
                "status": status,
                "active": status != 0,
                "raw": r.hex(),
            })
        return {"ok": True, "ts": time.time(), "faults": faults}
    finally:
        bus.shutdown()


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


dtc_lock = asyncio.Lock()


async def readdtc_handler(request):
    if state["can_status"] != "ok":
        return web.json_response({"ok": False, "error": "no CAN adapter"})
    if dtc_lock.locked():
        return web.json_response({"ok": False, "error": "read in progress"})
    async with dtc_lock:
        result = await asyncio.to_thread(uds_read_faults)
    state["dtc"] = result
    return web.json_response(result)


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
    app.router.add_post("/readdtc", readdtc_handler)
    app.router.add_get("/log.csv", csv_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    await asyncio.gather(can_lifecycle(), broadcaster(), csv_logger())


if __name__ == "__main__":
    asyncio.run(main())
