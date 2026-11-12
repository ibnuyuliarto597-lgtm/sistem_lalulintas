"""PLC lampu lalu lintas 4 arah (state machine 12 fase + TON + interlock) dan gateway WebSocket.

Pemetaan I/O (sama dengan panel HMI):
  Coil 0 START | Coil 1 STOP | Coil 2 MODE_AUTO | Coil 3 DARURAT | Coil 4 LANGKAH (manual)
  Output 0..11 = N_M N_K N_H | E_M E_K E_H | S_M S_K S_H | W_M W_K W_H

Jalankan:  python server.py          (port 8000)
Opsional:  ALLOWED_ORIGIN=https://domainanda.com python server.py   (hanya terima WebSocket dari situs Anda)
"""
import asyncio, json, os, time
from pathlib import Path
from aiohttp import web, WSMsgType

BASE = Path(__file__).parent
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "")
DUR = {"g": 20, "y": 3, "ar": 1}                   # detik, bisa diubah dari HMI
ALL_RED = (1, 0, 0) * 4


def phase_dur(f):                                  # fase 0..11: tiap arah = hijau, kuning, all-red
    return (DUR["g"], DUR["y"], DUR["ar"])[f % 3]


def phase_out(f):
    o = list(ALL_RED)
    d, k = divmod(f, 3)                            # d: 0=N 1=E 2=S 3=W
    if k == 0: o[d*3:d*3+3] = [0, 0, 1]            # hijau
    elif k == 1: o[d*3:d*3+3] = [0, 1, 0]          # kuning
    return tuple(o)


class TON:
    """Timer on-delay: Q = TRUE setelah IN aktif selama PT detik."""
    def __init__(self): self.et = 0.0
    def __call__(self, IN, PT, dt):
        self.et = self.et + dt if IN else 0.0
        return IN and self.et >= PT
    def reset(self): self.et = 0.0


class PLC:
    def __init__(self):
        self.run, self.auto, self.emg, self.step = True, True, False, False
        self.fase, self.timer, self.out, self.itl = 0, TON(), phase_out(0), False

    def write_coil(self, i, v):
        if i == 0 and v: self.run, self.emg = True, False     # START
        elif i == 1 and v: self.run = False                   # STOP
        elif i == 2: self.auto = bool(v)                      # MODE_AUTO
        elif i == 3: self.emg = bool(v)                       # DARURAT
        elif i == 4 and v: self.step = True                   # LANGKAH

    def scan(self, dt):
        self.itl = False
        if self.emg:                                          # darurat: kuning berkedip 4 arah
            b = int(time.time() * 2) % 2
            self.out = (0, b, 0) * 4
            self.fase = 2; self.timer.reset(); return
        if not self.run:                                      # stop: semua merah
            self.out = ALL_RED; self.fase = 2; self.timer.reset(); return
        done = self.timer(True, phase_dur(self.fase), dt) if self.auto else False
        if done or (not self.auto and self.step):             # CASE fase: lanjut bila TON.Q
            self.fase = (self.fase + 1) % 12
            self.timer.reset()
        self.step = False
        self.out = phase_out(self.fase)
        if sum(self.out[2::3]) > 1:                           # INTERLOCK: hijau tak boleh bersamaan
            self.out = ALL_RED; self.itl = True

    def snapshot(self):
        return {"fase": self.fase, "rem": max(0.0, phase_dur(self.fase) - self.timer.et),
                "out": list(self.out), "run": self.run, "auto": self.auto,
                "emg": self.emg, "itl": self.itl}


plc = PLC()
clients = set()


async def scan_loop():
    last = time.monotonic()
    while True:
        await asyncio.sleep(0.1)
        now = time.monotonic()
        plc.scan(now - last); last = now
        msg = json.dumps(plc.snapshot())
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
            if "coil" in d: plc.write_coil(int(d["coil"]), d["v"])
            if "cfg" in d:
                for k in ("g", "y", "ar"):
                    if k in d["cfg"]: DUR[k] = max(0, min(60, int(d["cfg"][k])))
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
