from ldm.segments import Part, split_even, steal


def test_split_even_covers_file_contiguously():
    parts = split_even(10, 3, 1)
    assert [(p.first, p.last) for p in parts] == [(0, 2), (3, 5), (6, 9)]


def test_split_even_respects_min_part():
    assert len(split_even(100, 8, 50)) == 2
    assert len(split_even(10, 8, 50)) == 1


def test_steal_halves_largest_remaining_part():
    busy = Part(0, 99, done=10, active=True)
    small = Part(100, 119, done=0, active=True)
    parts = [busy, small]
    tail = steal(parts, 10)
    assert (busy.first, busy.last) == (0, 54)
    assert (tail.first, tail.last) == (55, 99)
    assert parts == [busy, tail, small]


def test_steal_refuses_parts_below_twice_min_part():
    assert steal([Part(0, 99, done=90, active=True)], 10) is None
