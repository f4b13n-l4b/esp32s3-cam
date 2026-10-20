"""API HTTP et interface web des bénévoles."""

from __future__ import annotations

import csv
import io
import re
import secrets
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Config
from .pipeline import Pipeline
from .reconcile import format_elapsed, reconcile
from .store import Store, now_us

STATIC = Path(__file__).parent / "static"
BIB_RE = re.compile(r"^[0-9A-Za-z-]{1,8}$")


class RaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class StartIn(BaseModel):
    # Heure locale « HH:MM:SS(.d) » ; vide = maintenant
    time: str | None = Field(default=None, max_length=12)


class BibIn(BaseModel):
    bib: str
    before_id: int | None = None


class MoveIn(BaseModel):
    before_id: int | None = None


class VoidIn(BaseModel):
    voided: bool = True


class CloseIn(BaseModel):
    closed: bool = True


class PassageIn(BaseModel):
    count: int = Field(default=1, ge=1, le=20)
    time: str | None = Field(default=None, max_length=12)  # heure locale, vide = maintenant


class PassagePatch(BaseModel):
    count: int | None = Field(default=None, ge=0, le=20)
    voided: bool | None = None


def create_app(cfg: Config, store: Store, pipeline: Pipeline) -> FastAPI:
    app = FastAPI(title="Chrono arrivée", docs_url=None, redoc_url=None, openapi_url=None)
    tz = ZoneInfo(cfg.server.timezone)

    def auth(x_token: str = Header(default="")) -> None:
        if not secrets.compare_digest(x_token.encode(), cfg.server.volunteer_token.encode()):
            raise HTTPException(401, "code d'accès invalide")

    def race_or_404():
        race = store.current_race()
        if race is None:
            raise HTTPException(404, "aucune course créée")
        return race

    def parse_local(t: str | None) -> int:
        """« 14:03:27.4 » (heure locale du jour) -> µs epoch ; None -> maintenant."""
        if not t:
            return now_us()
        m = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,3}))?", t.strip())
        if not m:
            raise HTTPException(422, "format attendu HH:MM:SS ou HH:MM:SS.d")
        h, mi, s = int(m[1]), int(m[2]), int(m[3])
        if h > 23 or mi > 59 or s > 59:
            raise HTTPException(422, "heure invalide")
        frac = int((m[4] or "0").ljust(6, "0")[:6])
        today = datetime.now(tz).date()
        dt = datetime(today.year, today.month, today.day, h, mi, s, frac, tzinfo=tz)
        return int(dt.timestamp() * 1_000_000)

    def local_str(us: int | None) -> str:
        if us is None:
            return ""
        dt = datetime.fromtimestamp(us / 1e6, tz)
        return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 100000}"

    # -- lecture -----------------------------------------------------------

    @app.get("/api/state", dependencies=[Depends(auth)])
    def state():
        race = store.current_race()
        base = {"camera": pipeline.status(), "server_time": local_str(now_us())}
        if race is None:
            return {**base, "race": None}
        passages = store.passages(race["id"])
        bibs = store.bibs(race["id"])
        res = reconcile(passages, bibs, race["start_us"], store.participants())
        return {
            **base,
            "race": {
                "id": race["id"],
                "name": race["name"],
                "start": local_str(race["start_us"]),
                "started": race["start_us"] is not None,
                "closed": bool(race["closed"]),
            },
            "passages": [
                {
                    "id": p.id,
                    "time": local_str(p.ts_us),
                    "elapsed": format_elapsed(p.ts_us - race["start_us"]) if race["start_us"] else "",
                    "count": p.count,
                    "source": p.source,
                    "photo": bool(p.photo),
                    "voided": p.voided,
                }
                for p in passages
            ],
            "bibs": [{"id": b.id, "bib": b.bib, "voided": b.voided} for b in bibs],
            "ranking": [
                {
                    "rank": r.rank,
                    "bib": r.bib,
                    "bib_entry_id": r.bib_entry_id,
                    "passage_id": r.passage_id,
                    "elapsed": format_elapsed(r.elapsed_us),
                    "name": " ".join(filter(None, [r.participant.get("prenom"), r.participant.get("nom")])) if r.participant else "",
                    "categorie": (r.participant or {}).get("categorie") or "",
                    "flags": r.flags,
                }
                for r in res.rows
            ],
            "warnings": res.warnings,
            "counts": {"passages": res.slots, "bibs": res.bibs},
        }

    @app.get("/api/preview.jpg", dependencies=[Depends(auth)])
    def preview():
        jpeg = pipeline.preview_jpeg()
        if jpeg is None:
            raise HTTPException(503, "pas encore d'image caméra")
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/passages/{pid}/frames", dependencies=[Depends(auth)])
    def passage_frames(pid: int):
        race = race_or_404()
        p = store.get_passage(pid)
        if p is None or p["race_id"] != race["id"]:
            raise HTTPException(404, "passage introuvable")
        d = pipeline.passage_dir(race["id"], pid)
        names = sorted(f.name for f in d.glob("*.jpg")) if d.is_dir() else []
        return {
            "photo": p["photo"],
            "frames": [
                {"name": n, "time": local_str(int(n[:-4])), "delta_ms": (int(n[:-4]) - p["ts_us"]) // 1000}
                for n in names
            ],
        }

    @app.get("/api/passages/{pid}/frames/{name}", dependencies=[Depends(auth)])
    def passage_frame(pid: int, name: str):
        # Nom strictement numérique : aucune traversée de répertoire possible
        if not re.fullmatch(r"\d{10,20}\.jpg", name):
            raise HTTPException(400, "nom invalide")
        race = race_or_404()
        path = pipeline.passage_dir(race["id"], pid) / name
        if not path.is_file():
            raise HTTPException(404, "image introuvable")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/export.csv", dependencies=[Depends(auth)])
    def export():
        race = race_or_404()
        res = reconcile(store.passages(race["id"]), store.bibs(race["id"]), race["start_us"], store.participants())
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        w.writerow(["place", "dossard", "nom", "prenom", "categorie", "club", "temps", "temps_brut_s", "heure_passage", "alertes"])
        for r in res.rows:
            p = r.participant or {}
            w.writerow([
                r.rank, r.bib or "", p.get("nom") or "", p.get("prenom") or "", p.get("categorie") or "", p.get("club") or "",
                format_elapsed(r.elapsed_us), f"{r.elapsed_us / 1e6:.1f}" if r.elapsed_us is not None else "",
                local_str(r.ts_us), ",".join(r.flags),
            ])
        filename = re.sub(r"[^0-9A-Za-z_-]+", "_", race["name"]) or "course"
        return StreamingResponse(
            iter(["﻿" + out.getvalue()]),  # BOM : ouverture correcte des accents dans Excel
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="classement_{filename}.csv"'},
        )

    # -- écriture ----------------------------------------------------------

    @app.post("/api/race", dependencies=[Depends(auth)])
    def new_race(body: RaceIn):
        rid = store.create_race(body.name.strip())
        pipeline.detector.reset()
        return {"id": rid}

    @app.post("/api/race/start", dependencies=[Depends(auth)])
    def start(body: StartIn):
        race = race_or_404()
        store.set_start(race["id"], parse_local(body.time))
        return {"ok": True}

    @app.post("/api/race/close", dependencies=[Depends(auth)])
    def close(body: CloseIn):
        race = race_or_404()
        store.set_closed(race["id"], body.closed)
        return {"ok": True}

    @app.post("/api/bibs", dependencies=[Depends(auth)])
    def add_bib(body: BibIn):
        race = race_or_404()
        bib = body.bib.strip()
        if not BIB_RE.fullmatch(bib):
            raise HTTPException(422, "dossard invalide")
        try:
            return {"id": store.add_bib(race["id"], bib, body.before_id)}
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/api/bibs/{bid}/move", dependencies=[Depends(auth)])
    def move_bib(bid: int, body: MoveIn):
        race = race_or_404()
        try:
            store.move_bib(race["id"], bid, body.before_id)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        return {"ok": True}

    @app.post("/api/bibs/{bid}/void", dependencies=[Depends(auth)])
    def void_bib(bid: int, body: VoidIn):
        store.set_bib_voided(bid, body.voided)
        return {"ok": True}

    @app.post("/api/passages", dependencies=[Depends(auth)])
    def add_passage(body: PassageIn):
        race = race_or_404()
        if race["start_us"] is None:
            raise HTTPException(409, "la course n'est pas démarrée")
        pid = store.add_passage(race["id"], parse_local(body.time), body.count, "manuel")
        return {"id": pid}

    @app.patch("/api/passages/{pid}", dependencies=[Depends(auth)])
    def patch_passage(pid: int, body: PassagePatch):
        if store.get_passage(pid) is None:
            raise HTTPException(404, "passage introuvable")
        store.update_passage(pid, body.count, body.voided)
        return {"ok": True}

    # -- interface ---------------------------------------------------------

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        )
        return resp

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app

