"""C-11.5: row fingerprints distinguish SQLite storage classes and contents."""

import contextlib
import math
import sqlite3
import struct

import pytest
from hypothesis import example, given, settings, strategies as st

from subfleet import prune_decisions as pruning


# Include surrogate code points that st.text() normally excludes. SQLite's
# default text decoder cannot produce them, but the encoder must not alias a
# surrogate pair with the corresponding non-BMP character or fail to hash it.
TEXT = st.text(alphabet=st.characters(blacklist_categories=()), max_size=40)
COLUMNS = st.one_of(TEXT, st.sampled_from([
    "payload", "1", "", 'a"b', "nul\0name", "\ud800", "\ud800\udc00", "\U00010000",
]))
CELLS = st.one_of(
    st.none(),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.floats(allow_nan=False, allow_infinity=True, width=64),
    TEXT,
    st.binary(max_size=40),
)
ROWS = st.dictionaries(COLUMNS, CELLS, min_size=1, max_size=6)


@st.composite
def row_pairs(draw):
    left = draw(ROWS)
    mode = draw(st.sampled_from(["equal", "reordered", "cell", "column", "other"]))
    right = dict(left)
    if mode == "reordered":
        right = dict(reversed(list(left.items())))
    elif mode == "cell":
        right[draw(st.sampled_from(list(left)))] = draw(CELLS)
    elif mode == "column":
        old_name = draw(st.sampled_from(list(left)))
        new_name = draw(COLUMNS.filter(lambda name: name not in left))
        right[new_name] = right.pop(old_name)
    elif mode == "other":
        right = draw(ROWS)
    return left, right


def same_value_and_storage_class(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, float) and left == right == 0:
        return math.copysign(1, left) == math.copysign(1, right)
    return left == right


@settings(max_examples=500, deadline=None, derandomize=True, database=None)
@given(pair=row_pairs())
@example(pair=({"payload": "blob:00ff"}, {"payload": b"\x00\xff"}))
@example(pair=({"payload": 1}, {"payload": 1.0}))
@example(pair=({"payload": "1"}, {"payload": 1}))
@example(pair=({"payload": "\ud800\udc00"}, {"payload": "\U00010000"}))
@example(pair=({"\ud800\udc00": None}, {"\U00010000": None}))
@example(pair=({"payload": "\0"}, {"payload": "\\u0000"}))
@example(pair=({"payload": -0.0}, {"payload": 0.0}))
@example(pair=({"payload": float("inf")}, {"payload": "Infinity"}))
@example(pair=({"payload": float("-inf")}, {"payload": "-Infinity"}))
def test_row_digest_is_injective_over_values_types_and_column_names(pair):
    """Equal digests require identical names, storage classes and values."""
    left, right = pair
    equal = left.keys() == right.keys() and all(
        same_value_and_storage_class(left[name], right[name]) for name in left
    )
    assert (pruning._row_digest(left) == pruning._row_digest(right)) == equal


@pytest.mark.parametrize("left,right", [
    (None, "null"),
    (1, 1.0),
    ("1", 1),
    ("blob:00ff", b"\x00\xff"),
    ("", b""),
    (float("inf"), "Infinity"),
    (float("-inf"), "-Infinity"),
    ("\ud800", "\\ud800"),
    ("\ud800\udc00", "\U00010000"),
    ("\0", "\\u0000"),
    (-0.0, 0.0),
])
def test_distinct_cells_have_distinct_digests(left, right):
    assert pruning._row_digest({"cell": left}) != pruning._row_digest({"cell": right})


@settings(max_examples=200, deadline=None, derandomize=True, database=None)
@given(values=st.lists(CELLS, min_size=1, max_size=20))
@example(values=[None, 1, 1.0, "1", "blob:00ff", b"\x00\xff", "nul\0text",
                 float("inf"), float("-inf"), float("nan")])
def test_table_fingerprint_is_deterministic_on_repeated_sqlite_reads(values):
    # Surrogates cannot be bound as SQLite TEXT by the default sqlite3 adapter;
    # they are exercised directly by the injectivity property above.
    with contextlib.closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE cells ("mixed name", "a""b")')
        for index, value in enumerate(values):
            if isinstance(value, str):
                try:
                    value.encode("utf-8")
                except UnicodeEncodeError:
                    continue
            conn.execute("INSERT INTO cells VALUES (?, ?)", (index, value))
        first = pruning._fingerprints(conn)
        assert first == pruning._fingerprints(conn)
        # The multiset digest also ignores row order.
        assert first["cells"] == pruning._digest(
            conn.execute("SELECT * FROM cells ORDER BY rowid DESC")
        )


def test_sqlite_real_nan_is_null_and_infinities_remain_real():
    with contextlib.closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        rows = [conn.execute("SELECT ? AS cell, typeof(?) AS storage", (v, v)).fetchone()
                for v in [float("nan"), None, float("inf"), float("-inf")]]
    assert rows[0]["cell"] is None
    assert [row["storage"] for row in rows] == ["null", "null", "real", "real"]
    assert pruning._row_digest(rows[0]) == pruning._row_digest(rows[1])
    assert pruning._row_digest(rows[2]) != pruning._row_digest(rows[3])


def test_real_encoding_preserves_nan_payload_bits_if_passed_directly():
    # SQLite itself converts NaN to NULL. Preserve all IEEE-754 bits anyway
    # when a row-like mapping is passed directly, including distinct NaNs.
    values = [struct.unpack(">d", bytes.fromhex(bits))[0] for bits in [
        "7ff8000000000001", "7ff8000000000002", "fff8000000000001",
    ]]
    assert all(math.isnan(value) for value in values)
    assert len({pruning._row_digest({"cell": value}) for value in values}) == 3
