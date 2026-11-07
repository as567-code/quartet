import math

from quartet.db.sanitize import REPLACEMENT, sanitize_for_storage, sanitize_text, strip_nul


def test_strip_nul_only_removes_nul():
    assert strip_nul("a\x00b\ud800") == "ab\ud800"


def test_lone_surrogates_are_replaced_and_nul_removed():
    out = sanitize_text("a\x00b\ud800c\udfffd")
    assert out == f"ab{REPLACEMENT}c{REPLACEMENT}d"
    out.encode("utf-8")  # encodable


def test_recurses_through_keys_values_lists_and_tuples():
    value = {"k\x00ey\ud800": ["v\x00", ("t\udc00", 3, None, True)], "n": {"x": 1.5}}
    assert sanitize_for_storage(value) == {
        f"key{REPLACEMENT}": ["v", [f"t{REPLACEMENT}", 3, None, True]],
        "n": {"x": 1.5},
    }


def test_non_finite_floats_become_none():
    out = sanitize_for_storage({"a": math.nan, "b": [math.inf, -math.inf], "c": 2.0})
    assert out == {"a": None, "b": [None, None], "c": 2.0}
