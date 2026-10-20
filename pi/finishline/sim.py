"""Scène d'arrivée synthétique, vue du dessus, pour les tests et la fausse caméra.

Échelle par défaut : image 640 x 480 couvrant 5 m de large (128 px/m).
Un coureur vu du dessus ≈ ellipse de 45 cm (épaules) x 30 cm, plus un peu de
« bruit » (bras). Les coureurs traversent l'image de haut en bas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import cv2
import numpy as np

WIDTH, HEIGHT = 640, 480
PX_PER_M = WIDTH / 5.0


@dataclass
class Runner:
    t0: float  # instant (s) où le centre du coureur franchit la ligne
    x_m: float  # position latérale du centre (m, 0 = bord gauche)
    speed: float = 4.0  # m/s
    shade: int = 60  # niveau de gris du maillot / de la tête


@dataclass
class Lighting:
    t0: float
    t1: float
    delta: int  # variation de luminosité globale (nuage : négatif)


def default_script() -> tuple[list[Runner], list[Lighting]]:
    """Scénario de démonstration : isolés, duo collé, paquet de 5 et un nuage."""
    runners = [
        Runner(2.0, 2.5),
        Runner(4.0, 1.2, shade=200),  # maillot clair sur bitume sombre
        Runner(6.0, 2.0), Runner(6.0, 2.48, shade=90),  # duo épaule contre épaule
        # paquet de 5 sur toute la largeur, légèrement étalé dans le temps
        Runner(9.0, 0.6), Runner(9.05, 1.6, shade=180), Runner(9.1, 2.6),
        Runner(9.0, 3.5, shade=150), Runner(9.15, 4.4),
        Runner(12.0, 3.0, speed=2.5),  # coureur épuisé, plus lent
    ]
    lights = [Lighting(13.5, 15.0, -40)]
    return runners, lights


def background(seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.normal(115, 10, (HEIGHT, WIDTH)).astype(np.float32)
    base = cv2.GaussianBlur(base, (0, 0), 1.2)  # texture de bitume
    img = np.clip(base, 0, 255).astype(np.uint8)
    # Ligne d'arrivée peinte (blanche) au milieu de l'image
    cv2.rectangle(img, (0, HEIGHT // 2 - 6), (WIDTH, HEIGHT // 2 + 6), 235, -1)
    return img


def render(t: float, runners: list[Runner], lights: list[Lighting], bg: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    img = bg.astype(np.int16).copy()
    line_y = HEIGHT // 2
    for r in runners:
        y = line_y + (t - r.t0) * r.speed * PX_PER_M
        if -60 < y < HEIGHT + 60:
            cx = int(r.x_m * PX_PER_M)
            axes = (int(0.225 * PX_PER_M), int(0.15 * PX_PER_M))
            cv2.ellipse(img, (cx, int(y)), axes, 0, 0, 360, int(r.shade), -1)
            cv2.circle(img, (cx, int(y)), int(0.1 * PX_PER_M), int(r.shade) // 2, -1)  # tête
    for lg in lights:
        if lg.t0 <= t <= lg.t1:
            img += lg.delta
    img += rng.normal(0, 3, img.shape).astype(np.int16)  # bruit capteur
    return np.clip(img, 0, 255).astype(np.uint8)


def frames(
    duration: float = 16.0,
    fps: float = 15.0,
    t_start_us: int = 1_700_000_000_000_000,
    script: tuple[list[Runner], list[Lighting]] | None = None,
    seed: int = 1,
    phase: float = 0.0,
) -> Iterator[tuple[int, np.ndarray]]:
    """``phase`` (s) décale l'échantillonnage : les passages ne tombent plus pile sur une image."""
    runners, lights = script or default_script()
    bg = background(seed)
    rng = np.random.default_rng(seed + 1)
    n = int(duration * fps)
    for i in range(n):
        t = i / fps + phase
        yield t_start_us + int(t * 1e6), render(t, runners, lights, bg, rng)


def expected_count(script: tuple[list[Runner], list[Lighting]] | None = None, duration: float = 16.0) -> int:
    runners, _ = script or default_script()
    return sum(1 for r in runners if r.t0 < duration)


def encode(img: np.ndarray, quality: int = 80) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("encodage JPEG impossible")
    return buf.tobytes()
