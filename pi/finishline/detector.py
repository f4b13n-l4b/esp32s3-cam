"""Détection des franchissements d'une ligne virtuelle.

Principe
--------
On ne regarde qu'une bande horizontale de quelques pixels posée sur la ligne
d'arrivée (caméra en vue plongeante, coureurs traversant la bande de haut en bas
ou de bas en haut).

1. Fond de référence : moyenne glissante de la bande vide, mise à jour
   uniquement là où il n'y a personne (mise à jour sélective).
2. Différence image/fond, corrigée du décalage global de luminosité
   (nuage, réglage auto-exposition) par soustraction de la médiane.
3. Occupation par colonne -> segments contigus (« blobs ») = coureurs ou paquets.
4. Estimation du nombre de coureurs par blob à partir de sa largeur
   (``runner_width_px`` = largeur d'épaules vue d'au-dessus, à calibrer).
5. Suivi d'une image à l'autre par recouvrement de colonnes : un groupe de blobs
   reliés conserve le maximum de coureurs déjà comptés, ce qui évite de recompter
   deux coureurs qui se rapprochent puis s'écartent.

Coût : O(largeur x hauteur de bande) par image, soit ~3 000 pixels en 320 px de
large : quelques dizaines de microsecondes avec NumPy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class DetectorConfig:
    line_y: float = 0.5  # position de la ligne, fraction de la hauteur de l'image
    band_px: int = 14  # hauteur de la bande analysée (pixels, après redimensionnement)
    process_width: int = 320  # largeur de travail (les images sont réduites à cette largeur)
    threshold: int = 25  # écart de niveau de gris considéré comme « occupé »
    min_blob_px: int = 10  # largeur minimale d'un blob (filtre bruit, feuilles, ombres fines)
    col_fill: float = 0.25  # part de la hauteur de bande qui doit être occupée pour qu'une colonne compte
    gap_px: int = 3  # trous comblés à l'intérieur d'un blob (bras, jambes)
    runner_width_px: float = 28.0  # largeur d'un coureur vu du dessus, en pixels de travail
    lighting_ratio: float = 0.9  # au-delà de cette occupation : changement de lumière, pas des coureurs
    persist_frames: int = 3  # un blob disparu est gardé ce nombre d'images (anti-scintillement)
    bg_alpha: float = 0.05  # vitesse d'adaptation du fond
    warmup_frames: int = 10  # images d'apprentissage du fond avant de compter


@dataclass
class Crossing:
    ts_us: int
    count: int
    x_start: int  # en pixels de travail
    x_end: int


@dataclass
class _Track:
    x_start: int
    x_end: int
    counted: int
    missed: int = 0


@dataclass
class FrameResult:
    crossings: list[Crossing] = field(default_factory=list)
    blobs: list[tuple[int, int, int]] = field(default_factory=list)  # (x0, x1, coureurs)
    occupancy: float = 0.0
    lighting_event: bool = False


class LineCrossingDetector:
    def __init__(self, cfg: DetectorConfig | None = None):
        self.cfg = cfg or DetectorConfig()
        self._bg: np.ndarray | None = None
        self._frames = 0
        self._tracks: list[_Track] = []
        self.total = 0

    # -- API publique ------------------------------------------------------

    def reset(self) -> None:
        self._bg = None
        self._frames = 0
        self._tracks = []
        self.total = 0

    def band_rows(self, height: int) -> tuple[int, int]:
        c = self.cfg
        center = int(round(c.line_y * (height - 1)))
        y0 = max(0, center - c.band_px // 2)
        y1 = min(height, y0 + c.band_px)
        return y0, y1

    def process(self, gray: np.ndarray, ts_us: int) -> FrameResult:
        """Traite une image en niveaux de gris déjà à ``process_width`` de large."""
        if gray.ndim != 2:
            raise ValueError("image en niveaux de gris attendue")
        if gray.shape[1] != self.cfg.process_width:
            raise ValueError(
                f"largeur {gray.shape[1]} != process_width {self.cfg.process_width}"
            )
        y0, y1 = self.band_rows(gray.shape[0])
        band = gray[y0:y1].astype(np.float32)

        if self._bg is None or self._bg.shape != band.shape:
            self._bg = band.copy()
            self._frames = 0

        fg = self._foreground(band)
        self._frames += 1
        result = FrameResult()

        occ_cols = fg.mean(axis=0) > self.cfg.col_fill
        result.occupancy = float(occ_cols.mean())

        if self._frames <= self.cfg.warmup_frames:
            self._bg += (band - self._bg) * 0.3  # apprentissage rapide
            return result

        if result.occupancy > self.cfg.lighting_ratio:
            # Toute la ligne « change » d'un coup : variation d'éclairage.
            # On ré-apprend vite le fond et on ne compte rien.
            self._bg += (band - self._bg) * 0.5
            result.lighting_event = True
            return result

        # Mise à jour sélective : seules les zones vides font évoluer le fond.
        alpha = self.cfg.bg_alpha
        self._bg = np.where(fg, self._bg, self._bg + (band - self._bg) * alpha)

        blobs = self._blobs(occ_cols)
        result.crossings = self._track(blobs, ts_us)
        result.blobs = [(t.x_start, t.x_end, t.counted) for t in self._tracks if t.missed == 0]
        return result

    # -- étapes internes ---------------------------------------------------

    def _foreground(self, band: np.ndarray) -> np.ndarray:
        diff = band - self._bg
        diff -= np.median(diff)  # compense un décalage global de luminosité
        return np.abs(diff) > self.cfg.threshold

    def _blobs(self, occ: np.ndarray) -> list[tuple[int, int]]:
        c = self.cfg
        occ = occ.copy()
        # Fermeture 1D : comble les petits trous à l'intérieur d'un même coureur.
        idx = np.flatnonzero(occ)
        if idx.size:
            gaps = np.diff(idx)
            for i in np.flatnonzero((gaps > 1) & (gaps <= c.gap_px + 1)):
                occ[idx[i] : idx[i + 1]] = True
        # Segments contigus
        padded = np.concatenate(([False], occ, [False]))
        edges = np.flatnonzero(padded[1:] != padded[:-1])
        starts, ends = edges[0::2], edges[1::2]
        return [(int(s), int(e)) for s, e in zip(starts, ends) if e - s >= c.min_blob_px]

    def _estimate(self, width: int) -> int:
        return max(1, int(round(width / self.cfg.runner_width_px)))

    def _track(self, blobs: list[tuple[int, int]], ts_us: int) -> list[Crossing]:
        prev = self._tracks
        # Graphe de recouvrement blobs courants <-> pistes précédentes (± 1 px de tolérance)
        links = [
            [j for j, t in enumerate(prev) if s <= t.x_end and t.x_start <= e]
            for (s, e) in blobs
        ]
        # Composantes connexes (union-find sur indices de blobs et de pistes)
        parent = list(range(len(blobs) + len(prev)))

        def find(a: int) -> int:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for i, js in enumerate(links):
            for j in js:
                parent[find(i)] = find(len(blobs) + j)

        groups: dict[int, tuple[list[int], list[int]]] = {}
        for i in range(len(blobs)):
            groups.setdefault(find(i), ([], []))[0].append(i)
        for j in range(len(prev)):
            groups.setdefault(find(len(blobs) + j), ([], []))[1].append(j)

        new_tracks: list[_Track] = []
        crossings: list[Crossing] = []
        for b_idx, t_idx in groups.values():
            carried = sum(prev[j].counted for j in t_idx)
            if not b_idx:
                # Pistes sans blob : on les garde quelques images (scintillement)
                for j in t_idx:
                    t = prev[j]
                    if t.missed + 1 <= self.cfg.persist_frames:
                        new_tracks.append(_Track(t.x_start, t.x_end, t.counted, t.missed + 1))
                continue
            widths = [blobs[i][1] - blobs[i][0] for i in b_idx]
            estimate = sum(self._estimate(w) for w in widths)
            total = max(estimate, carried)
            if total > carried:
                new = total - carried
                x0 = min(blobs[i][0] for i in b_idx)
                x1 = max(blobs[i][1] for i in b_idx)
                crossings.append(Crossing(ts_us=ts_us, count=new, x_start=x0, x_end=x1))
                self.total += new
            # Répartition du compte entre les blobs du groupe, proportionnelle à la largeur
            shares = _apportion(total, widths)
            for i, n in zip(b_idx, shares):
                s, e = blobs[i]
                new_tracks.append(_Track(s, e, n))
        self._tracks = new_tracks
        return crossings


def _apportion(total: int, weights: list[int]) -> list[int]:
    """Répartit ``total`` en entiers proportionnellement aux poids (méthode du plus fort reste),
    avec au moins 1 par blob quand c'est possible."""
    n = len(weights)
    if n == 0:
        return []
    if total <= n:
        return [1 if i < total else 0 for i in range(n)]
    base = [1] * n
    rest = total - n
    wsum = float(sum(weights)) or 1.0
    raw = [rest * w / wsum for w in weights]
    floors = [int(r) for r in raw]
    remainder = rest - sum(floors)
    order = sorted(range(n), key=lambda i: raw[i] - floors[i], reverse=True)
    for i in order[:remainder]:
        floors[i] += 1
    return [b + f for b, f in zip(base, floors)]
