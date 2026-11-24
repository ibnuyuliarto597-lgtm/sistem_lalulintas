"""PLC lampu lalu lintas grid 2x2: 4 PLC (S1..S4), state machine 12 fase + timer, interlock, koordinasi antar-simpang.

Format sama dengan index.html (grid 2x2):
  Browser -> server : {"coil":0..4,"v":0/1,"scope":"ALL|SELECTED","intId":0..3}   perintah tombol
                      {"cfg":{"g":18,"y":3,"ar":1},"sync":"GREEN_WAVE"}            durasi dan mode koordinasi
                      {"inputs":[{"queueLoad":0..10,"violatorDetected":false} x4]} sensor lapangan dari browser
                      {"reset":true}                                              tombol Refresh
  Server -> browser : {"intersections":[{id,fase,rem,out[12],run,auto,emg,itl,inputs} x4]}  (10x per detik)
Coil 0 START | 1 STOP | 2 MODE_AUTO | 3 DARURAT | 4 LANGKAH.  Output 0..11 = N, E, S, W (merah, kuning, hijau).

Jalankan:  python server.py   (port 8000)      Opsional: ALLOWED_ORIGIN=https://domainanda.com python server.py
"""
import asyncio, json, os, time
from pathlib import Path
from aiohttp import web, WSMsgType

BASE = Path(__file__).parent
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "")
OFFSET = [0, 4, 4, 8]                                   # offset detik tiap simpang (green wave)
DUR = {"g": 18, "y": 3, "ar": 1}
SYNC = "GREEN_WAVE"                                     # GREEN_WAVE | SYNC | ALTERNATE | INDEPENDENT
ALL_RED = [1, 0, 0] * 4


def phase_dur(f): return (DUR["g"], DUR["y"], DUR["ar"])[f % 3]
def cycle_total(): return 4 * (DUR["g"] + DUR["y"] + DUR["ar"])


def phase_out(f):                                       # fase 0..11: tiap arah = hijau, kuning, all-red
    o = ALL_RED[:]
    d, k = divmod(f, 3)
    if k == 0: o[d*3:d*3+3] = [0, 0, 1]
    elif k == 1: o[d*3:d*3+3] = [0, 1, 0]
    return o


def map_time(elapsed):                                  # waktu siklus penuh -> (fase, detik di fase)
    total = max(1, cycle_total())
    t = elapsed % total
    for f in range(12):
        d = phase_dur(f)
        if t < d or f == 11: return f, min(t, d)
        t -= d
    return 0, 0


class PLC:
    def __init__(self, i):
        self.id, self.run, self.auto, self.emg, self.step = i, True, True, False, False
        self.fase, self.et, self.out, self.itl = 0, 0.0, phase_out(0), False
        self.inputs = {"queueLoad": 0, "violatorDetected": False}

    def cycle_elapsed(self):                            # posisi dalam siklus penuh
        return sum(phase_dur(f) for f in range(self.fase)) + self.et

    def snapshot(self):
        return {"id": self.id, "fase": self.fase, "rem": max(0.0, phase_dur(self.fase) - self.et),
                "out": self.out, "run": self.run, "auto": self.auto, "emg": self.emg,
                "itl": self.itl, "inputs": self.inputs}


plcs = [PLC(i) for i in range(4)]


def apply_sync():
    p0 = plcs[0]
    if SYNC == "SYNC":
        for p in plcs: p.fase, p.et = p0.fase, p0.et
    elif SYNC == "ALTERNATE":
        a, b = map_time(p0.cycle_elapsed()), map_time(p0.cycle_elapsed() + cycle_total() / 2)
        for i in (0, 3): plcs[i].fase, plcs[i].et = a
        for i in (1, 2): plcs[i].fase, plcs[i].et = b
    elif SYNC == "GREEN_WAVE":
        base = p0.cycle_elapsed()
        for i, p in enumerate(plcs): p.fase, p.et = map_time(base + OFFSET[i])


def scan(dt):
    blink = int(time.time() * 2) % 2
    for idx, p in enumerate(plcs):
        p.itl = False
        if p.emg:                                       # darurat: kuning berkedip
            p.out = [0, blink, 0] * 4; p.fase = 2; p.et = 0.0; p.step = False; continue
        if not p.run:                                   # stop: semua merah
            p.out = ALL_RED[:]; p.fase = 2; p.et = 0.0; p.step = False; continue
        if p.auto:                                      # hijau adaptif ringan bila antrean padat
            bonus = 0.15 if (p.fase % 3 == 0 and p.inputs["queueLoad"] >= 8) else 0
            p.et += dt * (1 + bonus)
        if p.et >= phase_dur(p.fase) or (not p.auto and p.step):
            p.fase = (p.fase + 1) % 12; p.et = 0.0
            if SYNC == "SYNC" and idx == 0:
                for j, o in enumerate(plcs):
                    if j and o.run and not o.emg: o.fase, o.et = p.fase, 0.0
        p.step = False
        if p.auto and idx > 0 and SYNC in ("GREEN_WAVE", "ALTERNATE"):
            shift = OFFSET[idx] if SYNC == "GREEN_WAVE" else (0 if idx == 3 else cycle_total() / 2)
            p.fase, p.et = map_time(plcs[0].cycle_elapsed() + shift)
        p.out = phase_out(p.fase)
        if p.out[2] + p.out[5] + p.out[8] + p.out[11] > 1:      # INTERLOCK
            p.out = ALL_RED[:]; p.itl = True


def write_coil(cmd, v, scope="SELECTED", int_id=0):
    ids = [0, 1, 2, 3] if scope == "ALL" else [max(0, min(3, int(int_id)))]
    for i in ids:
        p = plcs[i]
        if cmd == 0: p.run, p.emg = True, False
        elif cmd == 1: p.run = False
        elif cmd == 2:
            p.auto = bool(v)
            if p.auto: apply_sync()
        elif cmd == 3: p.emg = bool(v)
        elif cmd == 4 and not p.auto and p.run and not p.emg: p.step = True


def set_cfg(cfg, sync):                                 # abaikan bila tidak berubah (tiap pengunjung baru mengirim cfg)
    global SYNC
    new = {"g": max(2, min(60, int(cfg.get("g", DUR["g"])))), "y": max(1, min(10, int(cfg.get("y", DUR["y"])))),
           "ar": max(0, min(10, int(cfg.get("ar", DUR["ar"]))))}
    if sync not in ("GREEN_WAVE", "SYNC", "ALTERNATE", "INDEPENDENT"): sync = SYNC
    if new != DUR or sync != SYNC:
        DUR.update(new); SYNC = sync; apply_sync()


def reset_all():
    for i in range(4): plcs[i] = PLC(i)
    apply_sync()


clients = set()


async def scan_loop():
    last = time.monotonic()
    while True:
        await asyncio.sleep(0.1)
        now = time.monotonic(); scan(now - last); last = now
        msg = json.dumps({"intersections": [p.snapshot() for p in plcs]})
        for ws in list(clients):
            try: await ws.send_str(msg)
            except Exception: clients.discard(ws)


async def ws_handler(request):
    if ALLOWED_ORIGIN and request.headers.get("Origin", "") != ALLOWED_ORIGIN:
        return web.Response(status=403, text="origin ditolak")
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    clients.add(ws)
    async for m in ws:
        if m.type != WSMsgType.TEXT: continue
        try:
            d = json.loads(m.data)
            if "coil" in d: write_coil(int(d["coil"]), d.get("v"), d.get("scope", "SELECTED"), d.get("intId", 0))
            if "cfg" in d: set_cfg(d["cfg"], d.get("sync", SYNC))
            if "inputs" in d:
                for p, s in zip(plcs, d["inputs"]):
                    p.inputs = {"queueLoad": max(0, min(10, int(s.get("queueLoad", 0)))), "violatorDetected": bool(s.get("violatorDetected"))}
            if d.get("reset"): reset_all()
        except Exception:
            pass
    clients.discard(ws)
    return ws


async def health(_): return web.Response(text="ok")
async def index(_): return web.FileResponse(BASE / "index.html")
async def on_start(app): app["scan"] = asyncio.create_task(scan_loop())

app = web.Application()
app.add_routes([web.get("/", index), web.get("/ws", ws_handler), web.get("/health", health)])
app.on_startup.append(on_start)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8000)
