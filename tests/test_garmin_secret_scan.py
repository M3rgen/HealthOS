"""Deterministic work bounds and malformed-string recovery for credential scanning."""

import json

import pytest
from garmin_records import TOKEN
from test_garmin_token_redaction import KEY
from test_garmin_token_redaction import echo_body as echo_body

from app.garmin.client import body_contains_secret
from app.garmin.secret_scan import json_strings


@pytest.mark.parametrize("size", [4096, 8192, 16384, 32768, 1024 * 1024])
@pytest.mark.parametrize("shape", ["unterminated", "long-string", "many-strings", "recovery"])
def test_total_decoder_input_is_bounded_by_body_size(monkeypatch, size, shape):
    quoted = '"' + '\\"' * (size // 2)
    text = {
        "unterminated": quoted,
        "long-string": quoted + '"',
        "many-strings": '["\\u0061",' * (size // 10) + '"end"' + "]" * (size // 10),
        "recovery": quoted + '\n"\\u0061"',
    }[shape]
    decode = json.JSONDecoder.raw_decode
    total = 0

    def counted(decoder, source, idx=0):
        nonlocal total
        try:
            value, end = decode(decoder, source, idx)
        except ValueError:
            total += len(source) - idx
            assert total <= len(text)
            raise
        total += end - idx
        # Fail repeated suffix scans independently of machine speed.
        assert total <= len(text)
        return value, end

    monkeypatch.setattr(json.JSONDecoder, "raw_decode", counted)
    assert not body_contains_secret(text.encode(), [TOKEN, KEY])
    if shape == "unterminated":
        assert total == 0
    else:
        assert total > 0


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"])
@pytest.mark.parametrize("suffix", [b"", b' "' + b'\\"' * 16384], ids=["valid", "unterminated"])
def test_large_clean_body_and_late_secret_variants(encoding, suffix, echo_body):
    prefix = '[{"padding":' + json.dumps("clean\\u0000" * 65536) + ',"debug":null},'
    literal = json.dumps({"debug": r"\u0073" + TOKEN[1:]})
    clean = (prefix + literal + "]" + suffix.decode()).encode(encoding)
    assert not body_contains_secret(clean, [TOKEN])
    for location in ("key", "value"):
        raw = (prefix + echo_body(location).decode() + "]" + suffix.decode()).encode(encoding)
        assert body_contains_secret(raw, [TOKEN, KEY])


@pytest.mark.parametrize(
    "broken",
    ['"\\q', '"\\u12', '"\\u"', '"\n', '"\x00', '"' + '\\"' * 16384],
    ids=["escape", "short-unicode", "quote-in-unicode", "newline", "nul", "many-quotes"],
)
def test_recovery_preserves_readable_strings_and_literal_escapes(broken):
    # A control character ends any still-open candidate before recovery.
    text = "broken " + broken + '\n{"\\u0073ecret":"\\\\u0073ecret","key":"\\u0073ecret"}'
    assert list(json_strings(text)) == ["secret", r"\u0073ecret", "key", "secret"]
    assert body_contains_secret(text.encode(), ["secret"])
    assert not body_contains_secret(b'{"debug":"\\\\u0073ecret"}', ["secret", ""])


@pytest.mark.parametrize("secret", ['quote"slash\\end', "line\nend", "çığ/💚"])
def test_json_escape_pairs_and_surrogates_match_without_changing_literal_text(secret):
    raw = json.dumps({"debug": secret}, ensure_ascii=True).encode()
    assert body_contains_secret(raw, [secret])
    assert list(json_strings(raw.decode())) == ["debug", secret]
