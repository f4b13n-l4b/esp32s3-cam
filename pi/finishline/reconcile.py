"""Rapprochement passages (temps, caméra) <-> dossards (ordre, saisie bénévole).

Chaque passage de ``count`` coureurs ouvre ``count`` places au même temps.
Les places sont triées par temps, les dossards par ordre de saisie, et on les
associe un à un. Les écarts sont signalés pour correction humaine, jamais
« devinés » silencieusement.

Arrondi : temps affichés à la seconde supérieure (règle usuelle du chronométrage
non électronique). La valeur brute est conservée dans l'export.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from .store import BibEntry, Passage


@dataclass
class Row:
    rank: int
    bib: str | None
    bib_entry_id: int | None
    passage_id: int | None
    ts_us: int | None
    elapsed_us: int | None
    participant: dict | None = None
    flags: list[str] = field(default_factory=list)


@dataclass
class Result:
    rows: list[Row]
    warnings: list[str]
    slots: int
    bibs: int


def format_elapsed(us: int | None, round_up: bool = True) -> str:
    if us is None:
        return ""
    if us < 0:
        return "-" + format_elapsed(-us, round_up)
    s = math.ceil(us / 1_000_000) if round_up else us // 1_000_000
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def reconcile(
    passages: list[Passage],
    bibs: list[BibEntry],
    start_us: int | None,
    participants: dict[str, dict] | None = None,
) -> Result:
    participants = participants or {}
    slots: list[tuple[int, int, int]] = []  # (ts_us, passage_id, index dans le paquet)
    for p in sorted((p for p in passages if not p.voided), key=lambda p: (p.ts_us, p.id)):
        for i in range(p.count):
            slots.append((p.ts_us, p.id, i))
    entries = [b for b in bibs if not b.voided]

    warnings: list[str] = []
    dup = [bib for bib, n in Counter(b.bib for b in entries).items() if n > 1]
    for bib in sorted(dup):
        warnings.append(f"Dossard {bib} saisi plusieurs fois")
    if len(slots) > len(entries):
        warnings.append(
            f"{len(slots) - len(entries)} passage(s) caméra sans dossard : saisie en retard ou faux positif"
        )
    elif len(entries) > len(slots):
        warnings.append(
            f"{len(entries) - len(slots)} dossard(s) sans passage caméra : coureur manqué "
            "(ajouter un passage manuel ou corriger le nombre d'un paquet)"
        )
    if start_us is None and slots:
        warnings.append("Heure de départ non renseignée : temps indisponibles")

    rows: list[Row] = []
    for rank in range(max(len(slots), len(entries))):
        slot = slots[rank] if rank < len(slots) else None
        entry = entries[rank] if rank < len(entries) else None
        flags: list[str] = []
        if entry is None:
            flags.append("dossard_manquant")
        if slot is None:
            flags.append("temps_manquant")
        if entry is not None and entry.bib in dup:
            flags.append("doublon")
        if entry is not None and participants and entry.bib not in participants:
            flags.append("dossard_inconnu")
        ts = slot[0] if slot else None
        rows.append(
            Row(
                rank=rank + 1,
                bib=entry.bib if entry else None,
                bib_entry_id=entry.id if entry else None,
                passage_id=slot[1] if slot else None,
                ts_us=ts,
                elapsed_us=(ts - start_us) if (ts is not None and start_us is not None) else None,
                participant=participants.get(entry.bib) if entry else None,
                flags=flags,
            )
        )
    for b in entries:
        if participants and b.bib not in participants:
            warnings.append(f"Dossard {b.bib} absent de la liste des participants")
    return Result(rows=rows, warnings=warnings, slots=len(slots), bibs=len(entries))
