from finishline.reconcile import format_elapsed, reconcile
from finishline.store import BibEntry, Passage

S = 1_000_000


def P(i, t, n=1, voided=False):
    return Passage(i, t * S, n, "camera", None, voided)


def B(i, bib, voided=False):
    return BibEntry(i, float(i), bib, 0, voided)


def test_pack_slots_share_time_and_follow_entry_order():
    res = reconcile([P(1, 100), P(2, 105, 3)], [B(1, "12"), B(2, "7"), B(3, "33"), B(4, "4")], start_us=0)
    assert [r.bib for r in res.rows] == ["12", "7", "33", "4"]
    assert [r.elapsed_us // S for r in res.rows] == [100, 105, 105, 105]
    assert res.warnings == []


def test_mismatch_is_flagged_not_guessed():
    res = reconcile([P(1, 100), P(2, 110)], [B(1, "5")], start_us=0)
    assert res.rows[1].flags == ["dossard_manquant"]
    assert any("sans dossard" in w for w in res.warnings)
    res = reconcile([P(1, 100)], [B(1, "5"), B(2, "6")], start_us=0)
    assert res.rows[1].flags == ["temps_manquant"]


def test_voided_entries_ignored_and_duplicates_flagged():
    res = reconcile([P(1, 1), P(2, 2, voided=True), P(3, 3)], [B(1, "9"), B(2, "9"), B(3, "1", voided=True)], start_us=0)
    assert res.slots == 2 and res.bibs == 2
    assert all("doublon" in r.flags for r in res.rows)


def test_unknown_bib_flagged_when_participants_loaded():
    res = reconcile([P(1, 1)], [B(1, "999")], start_us=0, participants={"1": {"nom": "X"}})
    assert "dossard_inconnu" in res.rows[0].flags


def test_format_elapsed_rounds_up():
    assert format_elapsed(0) == "0:00:00"
    assert format_elapsed(59_000_001) == "0:01:00"
    assert format_elapsed(3_725_400_000) == "1:02:06"
    assert format_elapsed(None) == ""
