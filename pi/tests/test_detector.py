import cv2
import numpy as np
import pytest

from finishline import sim
from finishline.detector import DetectorConfig, LineCrossingDetector, _apportion


def run(script=None, fps=15.0, duration=16.0, phase=0.0, **cfg):
    det = LineCrossingDetector(DetectorConfig(**cfg))
    crossings, lighting = [], 0
    for ts, img in sim.frames(duration=duration, fps=fps, script=script, phase=phase):
        gray = cv2.resize(img, (320, 240), interpolation=cv2.INTER_AREA)
        res = det.process(gray, ts)
        crossings += res.crossings
        lighting += res.lighting_event
    return det, crossings, lighting


@pytest.mark.parametrize("fps", [12.0, 15.0, 20.0])
@pytest.mark.parametrize("phase_frac", [0.0, 0.3, 0.7])
def test_default_scenario_counts_every_runner(fps, phase_frac):
    # phase : les passages ne tombent pas pile sur une image (cas réel)
    det, crossings, _ = run(fps=fps, phase=phase_frac / fps)
    assert det.total == sim.expected_count()


def test_pack_of_five_reported_as_one_group():
    det, crossings, _ = run()
    t0 = 1_700_000_000_000_000
    pack = [c for c in crossings if 8.9 <= (c.ts_us - t0) / 1e6 <= 9.5]
    assert sum(c.count for c in pack) == 5


def test_cloud_does_not_create_runners():
    script = ([], [sim.Lighting(2.0, 4.0, -45), sim.Lighting(6.0, 8.0, +35)])
    det, crossings, _ = run(script=script, duration=10.0)
    assert det.total == 0


def test_runner_standing_on_line_counted_once():
    # Coureur très lent (0,3 m/s) : il reste plusieurs secondes sur la ligne
    script = ([sim.Runner(3.0, 2.5, speed=0.3)], [])
    det, _, _ = run(script=script, duration=8.0)
    assert det.total == 1


def test_crossing_time_matches_line():
    script = ([sim.Runner(3.0, 2.5, speed=4.0)], [])
    _, crossings, _ = run(script=script, duration=6.0, fps=20.0)
    assert len(crossings) == 1
    t = (crossings[0].ts_us - 1_700_000_000_000_000) / 1e6
    # Détecté quand le buste atteint la bande : au plus ~0,1 s avant/après le centre
    assert abs(t - 3.0) < 0.15


def test_rejects_wrong_width():
    det = LineCrossingDetector()
    with pytest.raises(ValueError):
        det.process(np.zeros((240, 640), np.uint8), 0)


@pytest.mark.parametrize("total,weights,expected", [
    (2, [30, 30], [1, 1]),
    (5, [60, 90], [2, 3]),
    (1, [10, 10], [1, 0]),
    (3, [10], [3]),
])
def test_apportion(total, weights, expected):
    out = _apportion(total, weights)
    assert sum(out) == total
    assert out == expected
