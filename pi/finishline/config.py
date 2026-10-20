"""Chargement de la configuration TOML."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .detector import DetectorConfig


@dataclass
class CameraConfig:
    base_url: str = "http://10.42.0.20"  # port 80 : /status, /capture
    stream_url: str = "http://10.42.0.20:81/stream"
    token: str = ""
    max_clock_skew_ms: int = 1500  # au-delà : l'horloge caméra est jugée non synchronisée


@dataclass
class RecordingConfig:
    data_dir: str = "./data"
    buffer_s: float = 10.0  # mémoire tampon d'images en RAM
    photo_pre_s: float = 0.5  # images conservées avant chaque passage
    photo_post_s: float = 1.0  # et après
    record_all: bool = False  # enregistre TOUTES les images pendant la course (~1,5 Go/h en VGA)


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    volunteer_token: str = ""  # code d'accès des bénévoles (obligatoire)
    timezone: str = "Europe/Paris"
    participants_csv: str = ""


@dataclass
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


def _fill(cls, data: dict):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"clés inconnues dans [{cls.__name__}] : {', '.join(sorted(unknown))}")
    return cls(**data)


def load(path: str | Path) -> Config:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    cfg = Config(
        camera=_fill(CameraConfig, raw.get("camera", {})),
        detector=_fill(DetectorConfig, raw.get("detector", {})),
        recording=_fill(RecordingConfig, raw.get("recording", {})),
        server=_fill(ServerConfig, raw.get("server", {})),
    )
    if len(cfg.server.volunteer_token) < 6:
        raise ValueError("server.volunteer_token doit faire au moins 6 caractères")
    return cfg
