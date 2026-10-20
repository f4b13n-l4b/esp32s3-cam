"""Lecture d'un flux MJPEG multipart dont chaque image porte un en-tête X-Timestamp."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


@dataclass
class Frame:
    ts_us: int  # instant de capture côté caméra (µs epoch UTC), 0 si absent
    jpeg: bytes


class MjpegParser:
    """Analyseur incrémental : on lui donne des octets bruts, il rend des images complètes.

    Indépendant du transport (HTTP, fichier, tests). Utilise Content-Length pour
    découper : aucune recherche de marqueur JPEG dans les données binaires.
    """

    MAX_HEADER = 1024
    MAX_FRAME = 4 * 1024 * 1024  # garde-fou mémoire contre un flux corrompu ou hostile

    def __init__(self) -> None:
        self._buf = bytearray()
        self._need: int | None = None
        self._ts = 0

    def feed(self, data: bytes) -> Iterator[Frame]:
        self._buf += data
        while True:
            if self._need is None:
                end = self._buf.find(b"\r\n\r\n")
                if end < 0:
                    if len(self._buf) > self.MAX_HEADER * 4:
                        # Pas d'en-tête plausible : on jette pour resynchroniser
                        del self._buf[: -self.MAX_HEADER]
                    return
                header = bytes(self._buf[:end]).decode("latin-1", "replace")
                del self._buf[: end + 4]
                length, ts = self._parse_header(header)
                if length is None:
                    continue  # bloc sans Content-Length (préambule) : ignoré
                if not 0 < length <= self.MAX_FRAME:
                    raise ValueError(f"taille d'image invalide : {length}")
                self._need, self._ts = length, ts
            if len(self._buf) < self._need:
                return
            jpeg = bytes(self._buf[: self._need])
            del self._buf[: self._need]
            self._need = None
            yield Frame(self._ts, jpeg)

    @staticmethod
    def _parse_header(header: str) -> tuple[int | None, int]:
        length: int | None = None
        ts = 0
        for line in header.split("\r\n"):
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "content-length":
                length = int(value)
            elif key == "x-timestamp":
                ts = int(value)
        return length, ts
