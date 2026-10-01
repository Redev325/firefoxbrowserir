#!/usr/bin/env python3
"""Stream the PulseAudio monitor to a browser as low-latency PCM."""

import argparse
import ctypes
import ctypes.util
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from websockify.websocketproxy import ProxyRequestHandler, WebSocketProxy


PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Remote desktop</title>
  <style>
    :root { color-scheme: light; font: 16px/1.5 system-ui, sans-serif; }
    body { margin: 0; min-height: 100vh; background: #e8eee9; color: #18221d; }
    main { width: min(70rem, calc(100% - 2rem)); margin: 0 auto; padding: 1rem 0 2rem; }
    h1 { margin: 0 0 1rem; font-size: 1.5rem; }
    h2 { margin: 0 0 .75rem; font-size: 1.125rem; }
    #remote { display: block; width: 100%; height: min(64vh, 44rem); min-height: 16rem; border: 0; background: #101411; }
    section { padding: 1rem 0; border-bottom: 1px solid #b8c7bd; }
    .controls { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; }
    button { min-height: 2.75rem; padding: .6rem 1rem; border: 1px solid #145d3b; border-radius: 5px; background: #145d3b; color: #fff; font: inherit; cursor: pointer; }
    button.secondary { background: #fff; color: #145d3b; }
    button:disabled { opacity: .5; cursor: default; }
    .status { min-height: 1.5rem; margin: .75rem 0; color: #394b40; }
    label { display: flex; align-items: center; gap: .75rem; }
    input { width: 100%; accent-color: #145d3b; }
    .note { margin: 1.5rem 0 0; color: #536158; font-size: .875rem; }
    .mapping { margin: .5rem 0 0; color: #394b40; }
    @media (max-width: 600px) { main { width: calc(100% - 1rem); } #remote { height: 48vh; min-height: 14rem; } }
  </style>
</head>
<body>
  <main>
    <h1>Remote desktop</h1>
    <iframe id="remote" title="Remote desktop" src="/vnc.html?autoconnect=1&amp;path=websockify&amp;resize=scale" allow="autoplay"></iframe>
    <section aria-labelledby="audio-heading">
      <h2 id="audio-heading">Audio</h2>
      <div class="controls">
        <button id="start">Start audio</button>
        <button id="stop" class="secondary" disabled>Stop</button>
      </div>
      <p id="status" class="status" role="status" aria-live="polite">Ready</p>
      <label for="volume">Volume <input id="volume" type="range" min="0" max="1" step="0.01" value="1"></label>
    </section>
    <section aria-labelledby="controller-heading">
      <h2 id="controller-heading">Controller</h2>
      <div class="controls"><button id="controller-toggle" class="secondary">Enable controller</button></div>
      <p id="controller-status" class="status" role="status" aria-live="polite">Connect and wake a controller, then enable it.</p>
      <p class="mapping">Left stick or D-pad: WASD. A: Space. B: Escape. X: E. Y: Q. LB: Shift. RB: Ctrl. Start: Enter.</p>
    </section>
    <p class="note">Keep this port private. Anyone who can access it can listen to desktop audio.</p>
  </main>
  <script>
    const sampleRate = __SAMPLE_RATE__;
    const startButton = document.querySelector('#start');
    const stopButton = document.querySelector('#stop');
    const statusText = document.querySelector('#status');
    const volume = document.querySelector('#volume');
    const controllerButton = document.querySelector('#controller-toggle');
    const controllerStatus = document.querySelector('#controller-status');
    let controller;
    let audioContext;
    let gain;
    let reader;
    let controllerEnabled = false;
    let controllerFrame;
    let controllerKeys = new Set();
    let controllerRequests = Promise.resolve();

    const mappedKeys = {
      w: { keysym: 0x77, code: 'KeyW' },
      a: { keysym: 0x61, code: 'KeyA' },
      s: { keysym: 0x73, code: 'KeyS' },
      d: { keysym: 0x64, code: 'KeyD' },
      space: { keysym: 0x20, code: 'Space' },
      escape: { keysym: 0xff1b, code: 'Escape' },
      e: { keysym: 0x65, code: 'KeyE' },
      q: { keysym: 0x71, code: 'KeyQ' },
      shift: { keysym: 0xffe1, code: 'ShiftLeft' },
      control: { keysym: 0xffe3, code: 'ControlLeft' },
      enter: { keysym: 0xff0d, code: 'Enter' },
    };

    function setStatus(message) {
      statusText.textContent = message;
    }

    async function stopAudio(message = 'Stopped') {
      controller?.abort();
      controller = undefined;
      reader?.cancel().catch(() => {});
      reader = undefined;
      gain?.disconnect();
      gain = undefined;
      if (audioContext) {
        await audioContext.close();
        audioContext = undefined;
      }
      startButton.disabled = false;
      stopButton.disabled = true;
      setStatus(message);
    }

    function queueFrames(bytes, frames, nextStart) {
      const buffer = audioContext.createBuffer(2, frames, sampleRate);
      const left = buffer.getChannelData(0);
      const right = buffer.getChannelData(1);
      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      for (let frame = 0; frame < frames; frame++) {
        left[frame] = view.getInt16(frame * 4, true) / 32768;
        right[frame] = view.getInt16(frame * 4 + 2, true) / 32768;
      }
      const source = audioContext.createBufferSource();
      source.buffer = buffer;
      source.connect(gain);
      const startAt = Math.max(nextStart.value, audioContext.currentTime + 0.03);
      source.start(startAt);
      nextStart.value = startAt + frames / sampleRate;
    }

    async function startAudio() {
      startButton.disabled = true;
      stopButton.disabled = false;
      setStatus('Connecting…');
      try {
        audioContext = new AudioContext({ latencyHint: 'interactive' });
        await audioContext.resume();
        gain = audioContext.createGain();
        gain.gain.value = Number(volume.value);
        gain.connect(audioContext.destination);
        controller = new AbortController();
        const response = await fetch(`/audio/stream?now=${Date.now()}`, { signal: controller.signal });
        if (!response.ok || !response.body) {
          throw new Error(`Audio server returned ${response.status}`);
        }

        setStatus('Listening');
        reader = response.body.getReader();
        const frames = Math.round(sampleRate / 50);
        const frameBytes = frames * 4;
        let pending = new Uint8Array(0);
        const nextStart = { value: audioContext.currentTime + 0.1 };

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const combined = new Uint8Array(pending.length + value.length);
          combined.set(pending);
          combined.set(value, pending.length);
          let offset = 0;
          while (combined.length - offset >= frameBytes) {
            queueFrames(combined.subarray(offset, offset + frameBytes), frames, nextStart);
            offset += frameBytes;
          }
          pending = combined.slice(offset);
        }
        await stopAudio('Audio stream ended');
      } catch (error) {
        if (error.name === 'AbortError') return;
        await stopAudio(`Unable to play audio: ${error.message}`);
      }
    }

    startButton.addEventListener('click', startAudio);
    stopButton.addEventListener('click', () => stopAudio());
    volume.addEventListener('input', () => {
      if (gain) gain.gain.value = Number(volume.value);
    });

    function sendControllerRequest(path, body = '') {
      controllerRequests = controllerRequests.then(async () => {
        const response = await fetch(path, {
          method: 'POST',
          headers: { 'Content-Type': 'text/plain;charset=UTF-8' },
          body,
        });
        if (!response.ok) throw new Error(`Controller request failed (${response.status})`);
      }).catch(error => {
        controllerStatus.textContent = error.message;
      });
      return controllerRequests;
    }

    function releaseControllerKeys() {
      controllerKeys.clear();
      return sendControllerRequest('/audio/controller/reset');
    }

    function updateControllerKeys(nextKeys) {
      for (const key of controllerKeys) {
        if (!nextKeys.has(key)) sendControllerRequest('/audio/controller/key', JSON.stringify({ key, down: false }));
      }
      for (const key of nextKeys) {
        if (!controllerKeys.has(key)) sendControllerRequest('/audio/controller/key', JSON.stringify({ key, down: true }));
      }
      controllerKeys = nextKeys;
    }

    function pollController() {
      if (!controllerEnabled) return;
      const gamepad = Array.from(navigator.getGamepads()).find(item => item && item.connected);
      if (!gamepad) {
        controllerStatus.textContent = 'Waiting for Chrome to expose a controller to this tab. Keep this page open and press a controller button.';
        controllerFrame = requestAnimationFrame(pollController);
        return;
      }

      const nextKeys = new Set();
      const horizontal = gamepad.axes[0] || 0;
      const vertical = gamepad.axes[1] || 0;
      if (horizontal < -0.45 || gamepad.buttons[14]?.pressed) nextKeys.add('a');
      if (horizontal > 0.45 || gamepad.buttons[15]?.pressed) nextKeys.add('d');
      if (vertical < -0.45 || gamepad.buttons[12]?.pressed) nextKeys.add('w');
      if (vertical > 0.45 || gamepad.buttons[13]?.pressed) nextKeys.add('s');
      if (gamepad.buttons[0]?.pressed) nextKeys.add('space');
      if (gamepad.buttons[1]?.pressed) nextKeys.add('escape');
      if (gamepad.buttons[2]?.pressed) nextKeys.add('e');
      if (gamepad.buttons[3]?.pressed) nextKeys.add('q');
      if (gamepad.buttons[4]?.pressed) nextKeys.add('shift');
      if (gamepad.buttons[5]?.pressed) nextKeys.add('control');
      if (gamepad.buttons[9]?.pressed) nextKeys.add('enter');

      updateControllerKeys(nextKeys);
  const layout = gamepad.mapping === 'standard' ? 'standard layout' : 'unmapped; using Xbox-style button order';
  controllerStatus.textContent = `Controller active: ${gamepad.id} (${layout}; ${gamepad.buttons.length} buttons, ${gamepad.axes.length} axes)`;
      controllerFrame = requestAnimationFrame(pollController);
    }

    controllerButton.addEventListener('click', () => {
      if (controllerEnabled) {
        controllerEnabled = false;
        cancelAnimationFrame(controllerFrame);
        releaseControllerKeys();
        controllerButton.textContent = 'Enable controller';
        controllerStatus.textContent = 'Controller disabled.';
        return;
      }
      if (!navigator.getGamepads) {
        controllerStatus.textContent = 'This browser does not support game controllers.';
        return;
      }
      const gamepad = Array.from(navigator.getGamepads()).find(item => item && item.connected);
      controllerEnabled = true;
      controllerButton.textContent = 'Disable controller';
      if (gamepad) {
        pollController();
      } else {
        controllerStatus.textContent = 'Waiting for Chrome to expose a controller to this tab. Keep this page open and press a controller button.';
        controllerFrame = requestAnimationFrame(pollController);
      }
    });

    window.addEventListener('gamepadconnected', event => {
      controllerStatus.textContent = `Chrome detected: ${event.gamepad.id}. Enable the controller to map its buttons.`;
      if (controllerEnabled) {
        cancelAnimationFrame(controllerFrame);
        pollController();
      }
    });

    window.addEventListener('gamepaddisconnected', event => {
      releaseControllerKeys();
      controllerStatus.textContent = `Controller disconnected: ${event.gamepad.id}`;
    });

    window.addEventListener('pagehide', () => {
      navigator.sendBeacon('/audio/controller/reset', new Blob([], { type: 'text/plain' }));
    });
  </script>
</body>
</html>
"""


SETTINGS_CONTROLS = """<li><hr></li>
<li>
  <div class="noVNC_heading">Remote Audio</div>
  <div class="noVNC_remote_controls">
    <input id="start" type="button" value="Start audio" class="noVNC_submit">
    <input id="stop" type="button" value="Stop audio" class="noVNC_submit" disabled>
    <p id="status" class="noVNC_remote_status" role="status" aria-live="polite">Ready</p>
    <label for="volume">Volume:</label>
    <input id="volume" type="range" min="0" max="1" step="0.01" value="1">
  </div>
</li>
<li><hr></li>
<li>
  <div class="noVNC_heading">Controller</div>
  <div class="noVNC_remote_controls">
    <input id="controller-toggle" type="button" value="Enable controller" class="noVNC_submit">
    <p id="controller-status" class="noVNC_remote_status" role="status" aria-live="polite">Connect and wake a controller, then enable it.</p>
    <p>Left stick or D-pad: WASD. A: Space. B: Escape. X: E. Y: Q. LB: Shift. RB: Ctrl. Start: Enter.</p>
  </div>
</li>
"""

SETTINGS_SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]


KEYSYMS = {
    "w": 0x77,
    "a": 0x61,
    "s": 0x73,
    "d": 0x64,
    "space": 0x20,
    "escape": 0xFF1B,
    "e": 0x65,
    "q": 0x71,
    "shift": 0xFFE1,
    "control": 0xFFE3,
    "enter": 0xFF0D,
}


class AudioProxyHandler(ProxyRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html", "/vnc.html"):
            try:
              body = self.build_novnc_page("vnc.html")
            except (OSError, ValueError) as error:
                self.send_error(500, str(error))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/audio":
            body = PAGE.replace("__SAMPLE_RATE__", str(self.server.sample_rate)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/audio/stream":
            self.stream_audio()
        elif path == "/audio/health":
            body = json.dumps({"source": self.server.source, "sample_rate": self.server.sample_rate}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def build_novnc_page(self, filename):
        document = (Path(self.server.web) / filename).read_text(encoding="utf-8")
        settings_marker = '<li class="noVNC_heading">\n                        <img alt="" src="app/images/settings.svg"> Settings\n                    </li>'
        if settings_marker not in document or "</body>" not in document:
            raise ValueError(f"Could not find the noVNC Settings insertion point in {filename}")

        document = document.replace(settings_marker, settings_marker + SETTINGS_CONTROLS, 1)
        sample_rate = str(self.server.sample_rate)
        script = SETTINGS_SCRIPT.replace("__SAMPLE_RATE__", sample_rate)
        additions = (
            "<style>.noVNC_remote_controls{padding:.35rem 0}"
            ".noVNC_remote_controls input[type=range]{width:100%}"
            ".noVNC_remote_status{font-size:.9em;line-height:1.3}</style>"
            f"<script>{script}</script>"
        )
        document = document.replace("</body>", additions + "</body>", 1)
        return document.encode("utf-8")

    def do_POST(self):
        path = urlsplit(self.path).path
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if not origin or urlsplit(origin).hostname != urlsplit(f"//{host}").hostname:
            self.send_error(403, "Controller requests must be same-origin")
            return

        try:
            if path == "/audio/controller/reset":
                events = [(keysym, False) for keysym in KEYSYMS.values()]
            elif path == "/audio/controller/key":
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length > 1024:
                    self.send_error(413, "Controller request is too large")
                    return
                request = json.loads(self.rfile.read(content_length))
                key = request.get("key")
                down = request.get("down")
                if key not in KEYSYMS or not isinstance(down, bool):
                    self.send_error(400, "Invalid controller key event")
                    return
                events = [(KEYSYMS[key], down)]
            else:
                self.send_error(404)
                return

            self.send_key_events(events)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            self.send_error(503, str(error))
            return

        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_key_events(self, events):
        x11_name = ctypes.util.find_library("X11")
        xtst_name = ctypes.util.find_library("Xtst")
        if not x11_name or not xtst_name:
            raise OSError("X11 XTEST libraries are unavailable")

        x11 = ctypes.CDLL(x11_name)
        xtst = ctypes.CDLL(xtst_name)
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XKeysymToKeycode.restype = ctypes.c_uint
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        xtst.XTestFakeKeyEvent.restype = ctypes.c_int

        display = x11.XOpenDisplay(self.server.display_name.encode())
        if not display:
            raise OSError(f"Could not open X display {self.server.display_name}")
        try:
            for keysym, down in events:
                keycode = x11.XKeysymToKeycode(display, keysym)
                if not keycode or not xtst.XTestFakeKeyEvent(display, keycode, int(down), 0):
                    raise OSError(f"Could not send key event for keysym {keysym}")
            x11.XFlush(display)
        finally:
            x11.XCloseDisplay(display)

    def stream_audio(self):
        command = [
            "parec",
            f"--device={self.server.source}",
            "--raw",
            "--format=s16le",
            f"--rate={self.server.sample_rate}",
            "--channels=2",
        ]
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as error:
            self.send_error(503, str(error))
            return

        first_chunk = process.stdout.read(4096)
        if not first_chunk:
            error = process.stderr.read().decode(errors="replace").strip()
            process.wait()
            self.send_error(503, error or "PulseAudio returned no audio data")
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store, no-transform")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(first_chunk)
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    parser.add_argument("--port", type=int, default=6080)
    parser.add_argument("--web-root", default="/usr/share/novnc", help="noVNC static files")
    parser.add_argument("--vnc-host", default="localhost")
    parser.add_argument("--vnc-port", type=int, default=5900)
    parser.add_argument("--display", default=":1", help="X11 display receiving controller key events")
    parser.add_argument("--source", help="PulseAudio source; defaults to the default sink monitor")
    parser.add_argument("--rate", type=int, default=44100)
    args = parser.parse_args()

    if not shutil.which("parec"):
        parser.error("parec is required (install pulseaudio-utils)")
    source = args.source
    if not source:
      try:
        sink = subprocess.run(
          ["pactl", "get-default-sink"], check=True, capture_output=True, text=True
        ).stdout.strip()
      except (OSError, subprocess.CalledProcessError) as error:
        parser.error(f"could not find the default PulseAudio sink: {error}")
      source = f"{sink}.monitor"
      try:
        subprocess.run(
          ["pactl", "set-sink-mute", sink, "0"],
          check=True,
          capture_output=True,
          text=True,
        )
      except (OSError, subprocess.CalledProcessError) as error:
        parser.error(f"could not unmute the PulseAudio sink {sink}: {error}")

    server = WebSocketProxy(
        RequestHandlerClass=AudioProxyHandler,
        listen_host=args.host,
        listen_port=args.port,
        target_host=args.vnc_host,
        target_port=args.vnc_port,
        web=args.web_root,
    )
    server.source = source
    server.sample_rate = args.rate
    server.display_name = args.display
    print(f"noVNC available at http://{args.host}:{args.port}/; audio controls at /audio")
    server.start_server()


if __name__ == "__main__":
    main()
