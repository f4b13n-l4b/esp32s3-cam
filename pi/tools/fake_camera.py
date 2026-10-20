"""Fausse caméra : imite le firmware ESP32-S3 (mêmes URL, même jeton, même X-Timestamp)
en diffusant la scène d'arrivée simulée en boucle.

Permet de tester le Pi et l'interface sans matériel :

    python tools/fake_camera.py --token secret --fps 15
    # puis dans config.toml : stream_url = "http://127.0.0.1:8081/stream", token = "secret"
"""

from __future__ import annotations

import argparse
import hmac
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from finishline import sim  # noqa: E402


def make_handler(token: str, fps: float, loop_s: float):
    frames = [sim.encode(img, 85) for _, img in sim.frames(duration=loop_s, fps=fps)]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # silencieux
            pass

        def _authorized(self) -> bool:
            got = self.headers.get("Authorization", "")
            ok = hmac.compare_digest(got.encode(), f"Bearer {token}".encode())
            if not ok:
                self.send_response(401)
                self.send_header("WWW-Authenticate", "Bearer")
                self.end_headers()
            return ok

        def do_GET(self):
            if not self._authorized():
                return
            if self.path == "/status":
                body = b'{"time_synced":true,"sensor_pid":9794}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frame")
                self.end_headers()
                period = 1.0 / fps
                i = 0
                try:
                    while True:
                        t0 = time.time()
                        jpeg = frames[i % len(frames)]
                        ts = time.time_ns() // 1000
                        self.wfile.write(
                            b"\r\n--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\nX-Timestamp: %d\r\n\r\n"
                            % (len(jpeg), ts)
                        )
                        self.wfile.write(jpeg)
                        i += 1
                        time.sleep(max(0.0, period - (time.time() - t0)))
                except (BrokenPipeError, ConnectionResetError):
                    return
            else:
                self.send_response(404)
                self.end_headers()

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--token", required=True)
    ap.add_argument("--fps", type=float, default=15.0)
    ap.add_argument("--loop", type=float, default=16.0, help="durée de la scène avant de reboucler (s)")
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.token, args.fps, args.loop))
    print(f"Fausse caméra sur http://{args.host}:{args.port}/stream ({args.fps} i/s)")
    server.serve_forever()


if __name__ == "__main__":
    main()
