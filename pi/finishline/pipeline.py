"""Traitement des images : décodage, détection, photos de passage, enregistrement.

``Pipeline.on_frame`` est indépendant du transport réseau : la même fonction est
appelée par le lecteur du flux caméra, par le simulateur et par les tests.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .config import Config
from .detector import FrameResult, LineCrossingDetector
from .mjpeg import Frame
from .store import Store, now_us

log = logging.getLogger(__name__)


@dataclass
class _Burst:
    passage_id: int
    ts_us: int
    until_us: int
    frames: list[Frame]


class Pipeline:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.detector = LineCrossingDetector(cfg.detector)
        self.data_dir = Path(cfg.recording.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._buffer: deque[Frame] = deque()
        self._bursts: list[_Burst] = []
        self._io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="photos")
        self._reduce_flag: int | None = None
        # État exposé à l'interface
        self.latest: Frame | None = None
        self.latest_result: FrameResult | None = None
        self.frames = 0
        self.fps = 0.0
        self.clock_skew_ms: float | None = None
        self.connected = False
        self.last_error: str | None = None
        self._last_recv = 0

    # -- entrée principale -------------------------------------------------

    def on_frame(self, frame: Frame, recv_us: int | None = None) -> FrameResult | None:
        recv_us = recv_us or now_us()
        if frame.ts_us == 0:
            frame = Frame(recv_us, frame.jpeg)  # pas d'horodatage caméra : repli sur la réception
        gray = self._decode(frame.jpeg)
        if gray is None:
            self.last_error = "image JPEG illisible"
            return None

        result = self.detector.process(gray, frame.ts_us)
        with self._lock:
            self._update_stats(frame, recv_us)
            self.latest, self.latest_result = frame, result
            self._buffer.append(frame)
            horizon = frame.ts_us - int(self.cfg.recording.buffer_s * 1e6)
            while self._buffer and self._buffer[0].ts_us < horizon:
                self._buffer.popleft()

        race = self.store.current_race()
        armed = race is not None and race["start_us"] is not None and not race["closed"]
        if armed:
            if self.cfg.recording.record_all:
                self._io.submit(self._write_frame, race["id"], frame)
            for c in result.crossings:
                pid = self.store.add_passage(race["id"], c.ts_us, c.count, "camera")
                self._start_burst(pid, c.ts_us)
        self._feed_bursts(frame, race["id"] if race else None)
        return result

    # -- état / aperçu -----------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            age = (now_us() - self._last_recv) / 1e6 if self._last_recv else None
            skew = self.clock_skew_ms
            return {
                "connected": self.connected and age is not None and age < 3,
                "fps": round(self.fps, 1),
                "frames": self.frames,
                "last_frame_age_s": round(age, 1) if age is not None else None,
                "clock_skew_ms": round(skew) if skew is not None else None,
                "clock_ok": skew is not None and abs(skew) <= self.cfg.camera.max_clock_skew_ms,
                "last_error": self.last_error,
                "detector_total": self.detector.total,
                "occupancy": round(self.latest_result.occupancy, 2) if self.latest_result else None,
            }

    def preview_jpeg(self) -> bytes | None:
        """Dernière image avec la bande de détection et les blobs dessinés (réglage sur place)."""
        with self._lock:
            frame, result = self.latest, self.latest_result
        if frame is None:
            return None
        img = cv2.imdecode(np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        scale = w / self.cfg.detector.process_width
        ph = int(round(h / scale))
        y0, y1 = self.detector.band_rows(ph)
        cv2.rectangle(img, (0, int(y0 * scale)), (w - 1, int(y1 * scale)), (0, 200, 255), 2)
        if result:
            for x0, x1, n in result.blobs:
                p0 = (int(x0 * scale), int(y0 * scale) - 18)
                cv2.rectangle(img, (int(x0 * scale), int(y0 * scale) - 4), (int(x1 * scale), int(y1 * scale) + 4), (0, 255, 0), 2)
                cv2.putText(img, str(n), p0, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None

    def passage_dir(self, race_id: int, passage_id: int) -> Path:
        return self.data_dir / f"course_{race_id}" / f"passage_{passage_id}"

    def flush(self) -> None:
        """Termine les écritures en attente (tests, arrêt)."""
        self._io.shutdown(wait=True)
        self._io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="photos")

    # -- interne -----------------------------------------------------------

    def _decode(self, jpeg: bytes) -> np.ndarray | None:
        data = np.frombuffer(jpeg, np.uint8)
        flag = self._reduce_flag if self._reduce_flag is not None else cv2.IMREAD_GRAYSCALE
        gray = cv2.imdecode(data, flag)
        if gray is None:
            return None
        if self._reduce_flag is None:
            # Décodage JPEG réduit natif (bien moins coûteux qu'un redimensionnement)
            ratio = gray.shape[1] / self.cfg.detector.process_width
            self._reduce_flag = (
                cv2.IMREAD_REDUCED_GRAYSCALE_4 if ratio >= 4
                else cv2.IMREAD_REDUCED_GRAYSCALE_2 if ratio >= 2
                else cv2.IMREAD_GRAYSCALE
            )
        pw = self.cfg.detector.process_width
        if gray.shape[1] != pw:
            ph = int(round(gray.shape[0] * pw / gray.shape[1]))
            gray = cv2.resize(gray, (pw, ph), interpolation=cv2.INTER_AREA)
        return gray

    def _update_stats(self, frame: Frame, recv_us: int) -> None:
        if self._last_recv:
            dt = (recv_us - self._last_recv) / 1e6
            if dt > 0:
                inst = 1.0 / dt
                self.fps = inst if self.fps == 0 else self.fps * 0.9 + inst * 0.1
        self._last_recv = recv_us
        self.frames += 1
        skew = (recv_us - frame.ts_us) / 1000.0
        self.clock_skew_ms = skew if self.clock_skew_ms is None else self.clock_skew_ms * 0.9 + skew * 0.1

    def _start_burst(self, passage_id: int, ts_us: int) -> None:
        pre = int(self.cfg.recording.photo_pre_s * 1e6)
        post = int(self.cfg.recording.photo_post_s * 1e6)
        with self._lock:
            frames = [f for f in self._buffer if f.ts_us >= ts_us - pre]
        self._bursts.append(_Burst(passage_id, ts_us, ts_us + post, frames))

    def _feed_bursts(self, frame: Frame, race_id: int | None) -> None:
        still: list[_Burst] = []
        for b in self._bursts:
            if frame.ts_us > b.ts_us:  # l'image du passage elle-même est déjà dans la rafale
                b.frames.append(frame)
            if frame.ts_us >= b.until_us and race_id is not None:
                self._io.submit(self._write_burst, race_id, b)
            else:
                still.append(b)
        self._bursts = still

    def _write_burst(self, race_id: int, b: _Burst) -> None:
        try:
            d = self.passage_dir(race_id, b.passage_id)
            d.mkdir(parents=True, exist_ok=True)
            best: Path | None = None
            for f in b.frames:
                p = d / f"{f.ts_us}.jpg"
                p.write_bytes(f.jpeg)
                if f.ts_us == b.ts_us:
                    best = p
            if best is None and b.frames:
                best = d / f"{min(b.frames, key=lambda f: abs(f.ts_us - b.ts_us)).ts_us}.jpg"
            if best is not None:
                self.store.set_passage_photo(b.passage_id, best.name)
        except OSError as e:
            log.error("écriture des photos du passage %s impossible : %s", b.passage_id, e)

    def _write_frame(self, race_id: int, f: Frame) -> None:
        d = self.data_dir / f"course_{race_id}" / "images"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{f.ts_us}.jpg").write_bytes(f.jpeg)


class CameraReader(threading.Thread):
    """Lit le flux MJPEG de la caméra et alimente le pipeline. Se reconnecte seul."""

    def __init__(self, cfg: Config, pipeline: Pipeline):
        super().__init__(daemon=True, name="camera")
        self.cfg = cfg
        self.pipeline = pipeline
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        import httpx

        from .mjpeg import MjpegParser

        headers = {"Authorization": f"Bearer {self.cfg.camera.token}"}
        backoff = 1.0
        while not self._halt.is_set():
            try:
                timeout = httpx.Timeout(5.0, read=5.0)
                with httpx.stream("GET", self.cfg.camera.stream_url, headers=headers, timeout=timeout) as r:
                    if r.status_code == 401:
                        raise RuntimeError("jeton caméra refusé (401) : vérifier camera.token")
                    r.raise_for_status()
                    self.pipeline.connected = True
                    self.pipeline.last_error = None
                    backoff = 1.0
                    parser = MjpegParser()
                    for chunk in r.iter_bytes():
                        if self._halt.is_set():
                            return
                        for frame in parser.feed(chunk):
                            self.pipeline.on_frame(frame)
            except Exception as e:  # réseau, caméra redémarrée, etc.
                self.pipeline.last_error = str(e)
                log.warning("flux caméra interrompu : %s (nouvel essai dans %.0f s)", e, backoff)
            self.pipeline.connected = False
            self._halt.wait(backoff)
            backoff = min(backoff * 2, 10.0)
