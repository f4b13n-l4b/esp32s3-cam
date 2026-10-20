"""Lancement : python -m finishline --config config.toml"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from . import config as config_mod
from .app import create_app
from .pipeline import CameraReader, Pipeline
from .store import Store


def main() -> None:
    ap = argparse.ArgumentParser(description="Chronométrage de ligne d'arrivée")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--no-camera", action="store_true", help="démarre sans lire la caméra (essais d'interface)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = config_mod.load(args.config)
    Path(cfg.recording.data_dir).mkdir(parents=True, exist_ok=True)
    store = Store(Path(cfg.recording.data_dir) / "chrono.sqlite3")
    if cfg.server.participants_csv:
        n = store.load_participants_csv(cfg.server.participants_csv)
        logging.info("%d participants chargés", n)

    pipeline = Pipeline(cfg, store)
    reader = None
    if not args.no_camera:
        reader = CameraReader(cfg, pipeline)
        reader.start()

    app = create_app(cfg, store, pipeline)
    try:
        uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="warning")
    finally:
        if reader:
            reader.stop()
        pipeline.flush()
        store.close()


if __name__ == "__main__":
    main()
