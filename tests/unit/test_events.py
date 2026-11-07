import json
from collections.abc import Mapping

import pytest

from quartet.domain.events import EVENT_PAYLOAD_MAX_BYTES, check_event_payload


def _compact_size(payload: Mapping[str, object]) -> int:
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def test_limit_is_64_kib():
    assert EVENT_PAYLOAD_MAX_BYTES == 65_536


def test_payload_at_the_limit_is_accepted_and_copied():
    payload = {"a": "x" * (65_536 - len('{"a":""}'))}
    assert _compact_size(payload) == 65_536
    checked = check_event_payload(payload)
    assert checked == payload and checked is not payload


def test_one_byte_over_is_rejected_with_size_and_limit_only():
    payload = {"a": "s3cr3t" + "x" * (65_537 - len('{"a":""}') - 6)}
    assert _compact_size(payload) == 65_537
    with pytest.raises(ValueError, match="65537") as ei:
        check_event_payload(payload)
    assert "65536" in str(ei.value) and "s3cr3t" not in str(ei.value)


def test_size_counts_utf8_bytes_not_characters():
    payload = {"a": "é" * 32_765}  # 2 bytes each: 65,530 + 8 = 65,538 bytes, 32,773 chars
    assert _compact_size(payload) > 65_536 > len(json.dumps(payload, ensure_ascii=False))
    with pytest.raises(ValueError):
        check_event_payload(payload)


def test_unencodable_text_is_rejected_without_echo():
    with pytest.raises(ValueError) as ei:
        check_event_payload({"a": "ok\ud800"})
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in str(ei.value))


def test_mapping_input_returns_plain_dict():
    from types import MappingProxyType

    checked = check_event_payload(MappingProxyType({"k": 1}))
    assert type(checked) is dict and checked == {"k": 1}
