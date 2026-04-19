from src.tapes import TAPES, bucket_label, format_hms, smallest_fitting_tape


def test_smallest_fitting_tape_short():
    t = smallest_fitting_tape(30 * 60)
    assert t is not None
    assert t.total_sec == 46 * 60


def test_smallest_fitting_tape_exact():
    t = smallest_fitting_tape(54 * 60)
    assert t is not None
    assert t.total_sec == 54 * 60


def test_smallest_fitting_tape_overflow():
    assert smallest_fitting_tape(130 * 60) is None


def test_bucket_label_coverage():
    labels = {bucket_label(t.total_sec) for t in TAPES}
    assert "<=46" in labels
    assert any("70<x<=90" in l or l == "70<x<=90" for l in labels)
    assert bucket_label(9999 * 60) == ">120 (won't fit)"


def test_format_hms():
    assert format_hms(0) == "0:00"
    assert format_hms(59) == "0:59"
    assert format_hms(60) == "1:00"
    assert format_hms(3599) == "59:59"
    assert format_hms(3600) == "1:00:00"
    assert format_hms(3725) == "1:02:05"
