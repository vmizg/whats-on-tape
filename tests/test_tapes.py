from src.tapes import TAPES, bucket_label, format_hms, smallest_fitting_tape, stretch_tolerance_sec


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


def test_smallest_fitting_tape_uses_stretch_tolerance():
    """A 47-min album fits the 46-min tape thanks to its stretch tolerance."""
    t = smallest_fitting_tape(47 * 60)
    assert t is not None and t.total_sec == 46 * 60

    # 124-min album still fits the 120-min tape.
    t = smallest_fitting_tape(124 * 60)
    assert t is not None and t.total_sec == 120 * 60

    # 130 min is comfortably outside the stretch zone (120 + 5 = 125), so still no fit.
    assert smallest_fitting_tape(130 * 60) is None


def test_stretch_tolerance_sec_lookup_rules():
    assert stretch_tolerance_sec(30 * 60) == 120
    assert stretch_tolerance_sec(45 * 60) == 180
    assert stretch_tolerance_sec(120 * 60) == 300
    # Unknown capacity falls back to the largest key <= capacity (here, the 120-min entry).
    assert stretch_tolerance_sec(200 * 60) == 300
    # Below the smallest key but >0 picks the 0 baseline.
    assert stretch_tolerance_sec(10 * 60) == 60
    assert stretch_tolerance_sec(0) == 0


def test_format_hms():
    assert format_hms(0) == "0:00"
    assert format_hms(59) == "0:59"
    assert format_hms(60) == "1:00"
    assert format_hms(3599) == "59:59"
    assert format_hms(3600) == "1:00:00"
    assert format_hms(3725) == "1:02:05"
