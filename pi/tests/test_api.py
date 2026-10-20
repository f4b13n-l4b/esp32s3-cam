"""Bout en bout : scène simulée -> pipeline -> API -> classement et export."""

import cv2
import pytest
from fastapi.testclient import TestClient

from finishline import sim
from finishline.app import create_app
from finishline.config import Config, ServerConfig, RecordingConfig
from finishline.mjpeg import Frame
from finishline.pipeline import Pipeline
from finishline.store import Store

TOKEN = "benevole-2026"
H = {"X-Token": TOKEN}
T0 = 1_700_000_000_000_000


@pytest.fixture
def env(tmp_path):
    cfg = Config(
        server=ServerConfig(volunteer_token=TOKEN),
        recording=RecordingConfig(data_dir=str(tmp_path)),
    )
    store = Store(tmp_path / "db.sqlite3")
    pipe = Pipeline(cfg, store)
    client = TestClient(create_app(cfg, store, pipe))
    yield cfg, store, pipe, client
    store.close()


def feed(pipe, fps=15.0):
    for ts, img in sim.frames(fps=fps, phase=0.3 / fps):
        pipe.on_frame(Frame(ts, sim.encode(img)), recv_us=ts + 40_000)  # 40 ms de latence réseau
    pipe.flush()


def test_requires_token(env):
    *_, client = env
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers={"X-Token": "faux"}).status_code == 401
    assert client.get("/").status_code == 200  # la page elle-même ne contient aucune donnée


def test_full_race(env):
    cfg, store, pipe, client = env
    store.load_participants_csv(_participants(cfg))
    assert client.post("/api/race", json={"name": "Automnales 10 km"}, headers=H).status_code == 200
    rid = store.current_race()["id"]
    store.set_start(rid, T0 - 30 * 60 * 1_000_000)  # départ 30 min avant la scène simulée

    feed(pipe)

    for bib in ["12", "7", "33", "41", "5", "8", "19", "23", "2", "99"]:
        assert client.post("/api/bibs", json={"bib": bib}, headers=H).status_code == 200

    s = client.get("/api/state", headers=H).json()
    assert s["counts"] == {"passages": 10, "bibs": 10}
    assert s["warnings"] == []
    assert s["camera"]["clock_ok"] is True
    first = s["ranking"][0]
    assert first["bib"] == "12" and first["elapsed"] == "0:30:02" and first["name"] == "Léa Martin"
    # Le paquet de 5 (étalé sur 0,15 s) obtient des temps à au plus 1 s d'écart
    secs = [int(r["elapsed"].split(":")[2]) for r in s["ranking"][4:9]]
    assert max(secs) - min(secs) <= 1

    # Photos enregistrées autour de chaque passage
    pid = s["ranking"][0]["passage_id"]
    frames = client.get(f"/api/passages/{pid}/frames", headers=H).json()
    assert frames["photo"] and len(frames["frames"]) >= 10
    img = client.get(f"/api/passages/{pid}/frames/{frames['photo']}", headers=H)
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"

    csv_text = client.get("/api/export.csv", headers=H).content.decode("utf-8-sig")
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("place;dossard;nom")
    assert lines[1].startswith("1;12;Martin;Léa")
    assert len(lines) == 11


def test_corrections(env):
    cfg, store, pipe, client = env
    client.post("/api/race", json={"name": "Test"}, headers=H)
    client.post("/api/race/start", json={"time": "10:00:00"}, headers=H)
    ids = [client.post("/api/bibs", json={"bib": b}, headers=H).json()["id"] for b in ["1", "2", "3"]]
    # Dossard 3 arrivé avant le 2 : on le remonte
    client.post(f"/api/bibs/{ids[2]}/move", json={"before_id": ids[1]}, headers=H)
    order = [b["bib"] for b in client.get("/api/state", headers=H).json()["bibs"]]
    assert order == ["1", "3", "2"]
    # Saisie erronée annulée
    client.post(f"/api/bibs/{ids[0]}/void", json={"voided": True}, headers=H)
    # Passage manuel puis correction du nombre
    pid = client.post("/api/passages", json={"count": 1, "time": "10:41:12.5"}, headers=H).json()["id"]
    client.patch(f"/api/passages/{pid}", json={"count": 2}, headers=H)
    s = client.get("/api/state", headers=H).json()
    assert s["counts"] == {"passages": 2, "bibs": 2}
    assert [r["elapsed"] for r in s["ranking"]] == ["0:41:13", "0:41:13"]


@pytest.mark.parametrize("payload", [{"bib": "<script>"}, {"bib": ""}, {"bib": "123456789"}, {"bib": "1; DROP"}])
def test_bib_validation(env, payload):
    *_, client = env
    client.post("/api/race", json={"name": "Test"}, headers=H)
    assert client.post("/api/bibs", json=payload, headers=H).status_code == 422


def test_no_path_traversal(env):
    *_, client = env
    client.post("/api/race", json={"name": "Test"}, headers=H)
    for name in ["..%2F..%2Fdb.sqlite3", "..%2Fdb.sqlite3", "x.jpg"]:
        r = client.get(f"/api/passages/1/frames/{name}", headers=H)
        assert r.status_code in (400, 404)


def test_passages_not_recorded_before_start(env):
    cfg, store, pipe, client = env
    client.post("/api/race", json={"name": "Échauffement"}, headers=H)
    feed(pipe)
    assert client.get("/api/state", headers=H).json()["counts"]["passages"] == 0


def test_preview_has_overlay(env):
    cfg, store, pipe, client = env
    assert client.get("/api/preview.jpg", headers=H).status_code == 503
    feed(pipe)
    r = client.get("/api/preview.jpg", headers=H)
    assert r.status_code == 200
    import numpy as np
    img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape == (480, 640, 3)


def test_security_headers(env):
    *_, client = env
    r = client.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def _participants(cfg):
    import pathlib
    p = pathlib.Path(cfg.recording.data_dir) / "participants.csv"
    p.write_text("dossard;nom;prenom;categorie;club\n12;Martin;Léa;SEF;AC Nort\n7;Durand;Paul;M0H;\n33;Bernard;Ana;M1F;\n41;Petit;Jo;SEH;\n5;Roux;Éva;JUF;\n8;Morel;Luc;M2H;\n19;Simon;Inès;SEF;\n23;Michel;Tom;ESH;\n2;Garcia;Noé;M3H;\n99;Faure;Zoé;SEF;\n", encoding="utf-8")
    return p
