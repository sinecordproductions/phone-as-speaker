import asyncio
import hmac
import logging
import queue
import socket
import threading
import time
from fractions import Fraction

import numpy as np
import pyaudiowpatch as pyaudio
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame

PORT = 8080
OUT_RATE = 48000                 # WebRTC Opus likes 48 kHz
FRAME = 960                      # 20 ms at 48 kHz
FRAME_BYTES = FRAME * 2 * 2      # stereo, 16 bit
MAX_BUFFER = FRAME_BYTES * 10    # keep at most 200 ms, drop older audio for low delay
PIN = ""                         # empty means no PIN; the GUI sets this from settings
attempts: dict = {}              # phone ip -> (wrong tries, locked until)


# The engine talks to the GUI only through this queue, never touching widgets.
events: "queue.Queue[tuple]" = queue.Queue()


def emit(kind: str, *args) -> None:
    events.put((kind, *args))


def log(msg: str) -> None:
    line = f"{time.strftime('[%H:%M:%S]')} {msg}"
    print(line, flush=True)
    logging.getLogger("phonespeaker").info(msg)
    emit("log", line)


def local_ip() -> str:
    # Connecting a UDP socket sends nothing, it only reveals the Wi-Fi IP in use.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Loopback:
    """Records whatever the laptop is playing, via WASAPI loopback."""

    def __init__(self) -> None:
        self.pa = None
        self.stream = None
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.level = 0.0          # 0..1 loudness for the GUI meter
        self.last_cb = 0.0        # time of the last audio chunk, to detect silence
        self.in_rate = OUT_RATE
        self.in_ch = 2
        self.max_buffer = MAX_BUFFER   # latency preset sets this
        self.device_name = ""          # empty means the default speaker
        self.default_name = ""         # default speaker we are following, to notice changes

    def _find_device(self):
        if self.device_name:
            for lb in self.pa.get_loopback_device_info_generator():
                if lb["name"] == self.device_name:
                    return lb
            log("Chosen audio device not found, using the default one")
        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        dev = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        self.default_name = dev["name"]
        if not dev.get("isLoopbackDevice"):
            for lb in self.pa.get_loopback_device_info_generator():
                if dev["name"] in lb["name"]:
                    return lb
            raise RuntimeError("No loopback device found for the default speaker")
        return dev

    def start(self) -> None:
        if self.stream:
            return
        self.pa = pyaudio.PyAudio()
        dev = self._find_device()
        self.in_rate = int(dev["defaultSampleRate"])
        self.in_ch = int(dev["maxInputChannels"])
        with self.lock:
            self.buf.clear()
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.in_ch,
            rate=self.in_rate,
            input=True,
            input_device_index=dev["index"],
            frames_per_buffer=int(self.in_rate * 0.02),
            stream_callback=self._callback,
        )
        log(f"Capturing: {dev['name']} ({self.in_rate} Hz, {self.in_ch} ch)")

    def stop(self) -> None:
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
        if self.pa:
            self.pa.terminate()
            self.pa = None
        self.level = 0.0

    def _callback(self, in_data, frame_count, time_info, status):
        arr = np.frombuffer(in_data, dtype=np.int16).reshape(-1, self.in_ch)
        if self.in_ch == 1:
            arr = np.repeat(arr, 2, axis=1)
        elif self.in_ch > 2:
            arr = arr[:, :2]
        self.level = float(np.sqrt(np.mean((arr / 32768.0) ** 2)))
        self.last_cb = time.monotonic()
        if self.in_rate != OUT_RATE:
            n_out = int(len(arr) * OUT_RATE / self.in_rate)
            pos = np.arange(n_out) * self.in_rate / OUT_RATE
            idx = np.arange(len(arr))
            arr = np.stack(
                [np.interp(pos, idx, arr[:, 0]), np.interp(pos, idx, arr[:, 1])], axis=1
            ).astype(np.int16)
        with self.lock:
            self.buf += arr.tobytes()
            if len(self.buf) > self.max_buffer:
                del self.buf[: len(self.buf) - self.max_buffer]
        return (None, pyaudio.paContinue)

    def pull(self, n: int) -> bytes:
        with self.lock:
            if len(self.buf) >= n:
                out = bytes(self.buf[:n])
                del self.buf[:n]
                return out
        # Windows sends no data while the laptop is silent, so we fill with silence
        # to keep the stream clock running.
        return bytes(n)


def list_loopbacks() -> list:
    pa = pyaudio.PyAudio()
    try:
        return [d["name"] for d in pa.get_loopback_device_info_generator()]
    except Exception:  # noqa: BLE001
        return []
    finally:
        pa.terminate()


def default_output_name() -> str:
    pa = pyaudio.PyAudio()
    try:
        w = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        return pa.get_device_info_by_index(w["defaultOutputDevice"])["name"]
    except Exception:  # noqa: BLE001
        return ""
    finally:
        pa.terminate()


cap = Loopback()


class LoopbackTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._t0 = None
        self._pts = 0

    async def recv(self) -> AudioFrame:
        if self._t0 is None:
            self._t0 = time.monotonic()
        wait = self._t0 + self._pts / OUT_RATE - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        frame = AudioFrame(format="s16", layout="stereo", samples=FRAME)
        frame.planes[0].update(cap.pull(FRAME_BYTES))
        frame.sample_rate = OUT_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, OUT_RATE)
        self._pts += FRAME
        return frame


current = None  # only one phone at a time


async def offer(request: web.Request) -> web.Response:
    global current
    params = await request.json()
    agent = request.headers.get("User-Agent", "")[:70]

    if PIN:
        ip = request.remote or "?"
        tries, locked_until = attempts.get(ip, (0, 0.0))
        if time.monotonic() < locked_until:
            return web.Response(status=429, text="Too many wrong PINs, wait one minute")
        if not hmac.compare_digest(str(params.get("pin", "")), PIN):
            tries += 1
            # 5 wrong tries lock that phone for a minute, so guessing the PIN takes far too long
            attempts[ip] = (0, time.monotonic() + 60) if tries >= 5 else (tries, 0.0)
            log(f"Wrong PIN from {ip}")
            emit("warn", f"Wrong PIN tried from {ip}")
            return web.Response(status=403, text="Wrong PIN")
        attempts.pop(ip, None)

    old = current
    pc = RTCPeerConnection()
    current = pc
    if old:
        log("New phone connected, replacing the old one")
        await old.close()

    try:
        cap.start()
    except Exception as e:  # noqa: BLE001
        log(f"Audio capture error: {e}")
        emit("warn", f"Audio capture failed: {e}")
        await pc.close()
        current = None
        return web.Response(status=500, text=f"Audio capture error: {e}")

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        global current
        log(f"Phone connection: {pc.connectionState}")
        if current is pc:  # ignore events of a phone that was replaced
            emit("phone", pc.connectionState, agent)
        if pc.connectionState in ("failed", "closed"):
            await pc.close()
            if current is pc:
                current = None
                cap.stop()
                log("Phone gone, capture stopped")

    pc.addTrack(LoopbackTrack())
    await pc.setRemoteDescription(RTCSessionDescription(sdp=params["sdp"], type=params["type"]))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    log(f"Phone offer received from: {agent}")
    return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PhoneSpeaker</title>
<style>
body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;
justify-content:center;gap:22px;background:#111;color:#eee;font-family:system-ui,sans-serif}
button{font-size:22px;padding:18px 48px;border:0;border-radius:40px;background:#3b82f6;color:#fff}
button:disabled{opacity:.5}
#st{font-size:18px}.ok{color:#4ade80}.warn{color:#facc15}.bad{color:#f87171}
input{width:70vw}
#pin{width:160px;font-size:22px;text-align:center;padding:10px;border-radius:12px;border:1px solid #444;background:#222;color:#eee;display:none}
</style></head><body>
<h2>PhoneSpeaker</h2>
<input id="pin" type="tel" inputmode="numeric" maxlength="8" placeholder="PIN">
<button id="btn">Play</button>
<div id="st" class="warn">Tap Play to start</div>
<label>Volume <input id="vol" type="range" min="0" max="1" step="0.01" value="1"></label>
<audio id="au" autoplay playsinline></audio>
<script>
const btn=document.getElementById('btn'),st=document.getElementById('st'),
au=document.getElementById('au'),vol=document.getElementById('vol');
const NEED_PIN=__PIN__;
const pinEl=document.getElementById('pin');
let pc=null,wl=null;
if(NEED_PIN){
  pinEl.style.display='block';
  if(location.hash.length>1){pinEl.value=location.hash.slice(1);history.replaceState(null,'',location.pathname);}
}
vol.oninput=()=>au.volume=vol.value;
function setState(t,c){st.textContent=t;st.className=c;}
async function start(){
  btn.disabled=true;setState('Connecting...','warn');
  try{
    if(pc)pc.close();
    pc=new RTCPeerConnection();
    pc.addTransceiver('audio',{direction:'recvonly'});
    pc.ontrack=e=>{au.srcObject=e.streams[0];au.play().catch(()=>{});};
    pc.onconnectionstatechange=()=>{
      const s=pc.connectionState;
      if(s==='connected'){setState('Connected and playing','ok');btn.textContent='Reconnect';btn.disabled=false;}
      else if(s==='disconnected'||s==='failed'){setState('Disconnected','bad');btn.textContent='Reconnect';btn.disabled=false;}
    };
    await pc.setLocalDescription(await pc.createOffer());
    await new Promise(r=>{
      if(pc.iceGatheringState==='complete')return r();
      pc.addEventListener('icegatheringstatechange',()=>{if(pc.iceGatheringState==='complete')r();});
      setTimeout(r,2000);
    });
    const res=await fetch('/offer',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({sdp:pc.localDescription.sdp,type:pc.localDescription.type,pin:pinEl.value})});
    if(!res.ok)throw new Error(await res.text());
    await pc.setRemoteDescription(await res.json());
    try{wl=await navigator.wakeLock.request('screen');}catch(e){}
  }catch(e){setState('Error: '+e.message,'bad');btn.disabled=false;}
}
btn.onclick=start;
</script></body></html>"""


async def index(request: web.Request) -> web.Response:
    return web.Response(text=PAGE.replace("__PIN__", "true" if PIN else "false"), content_type="text/html")


async def watch_default_device() -> None:
    """If the user switches the default speaker, such as plugging in headphones, follow it."""
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(2)
        if cap.stream and not cap.device_name:
            name = await loop.run_in_executor(None, default_output_name)
            if name and name != cap.default_name:
                log(f"Default speaker changed to {name}, switching capture")
                try:
                    cap.stop()
                    cap.start()
                except Exception as e:  # noqa: BLE001
                    log(f"Could not switch audio device: {e}")
                    emit("warn", f"Could not switch audio device: {e}")


async def run_server(port: int, stop_event: asyncio.Event) -> None:
    global current
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/offer", offer)
    runner = web.AppRunner(app)
    await runner.setup()
    watcher = None
    try:
        await web.TCPSite(runner, "0.0.0.0", port).start()
        emit("server", True)
        log(f"Server running on port {port}")
        watcher = asyncio.create_task(watch_default_device())
        await stop_event.wait()
    finally:
        if watcher:
            watcher.cancel()
        old, current = current, None
        if old:
            await old.close()
        cap.stop()
        await runner.cleanup()
        emit("server", False)
        log("Server stopped")


async def main() -> None:  # command line mode, same as stage 1
    log(f"Open this on your phone: http://{local_ip()}:{PORT}")
    await run_server(PORT, asyncio.Event())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass