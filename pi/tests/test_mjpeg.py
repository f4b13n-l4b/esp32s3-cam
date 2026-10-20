import pytest

from finishline.mjpeg import MjpegParser


def part(ts, payload):
    return (b"\r\n--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\nX-Timestamp: %d\r\n\r\n"
            % (len(payload), ts)) + payload


def test_parses_frames_split_arbitrarily():
    stream = part(111, b"\xff\xd8AAAA\xff\xd9") + part(222, b"\xff\xd8" + b"\r\n\r\n" * 3 + b"\xff\xd9")
    p = MjpegParser()
    frames = []
    for i in range(0, len(stream), 7):  # découpage réseau quelconque
        frames += list(p.feed(stream[i:i + 7]))
    assert [f.ts_us for f in frames] == [111, 222]
    assert frames[1].jpeg.count(b"\r\n\r\n") == 3  # données binaires intactes


def test_rejects_absurd_length():
    p = MjpegParser()
    with pytest.raises(ValueError):
        list(p.feed(b"--frame\r\nContent-Length: 999999999\r\n\r\n"))
