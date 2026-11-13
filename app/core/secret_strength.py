"""How strong a secret must be. Standard library only: the bridge launcher imports it too."""

import math
import re
from collections import Counter
from itertools import pairwise

# What a generator produces (``secrets.token_hex(16)`` is the shortest the guides name).
MIN_GENERATED_SECRET_LENGTH = 32
# The previous floor, honoured only behind HEALTHOS_ALLOW_LEGACY_SECRETS.
MIN_LEGACY_SECRET_LENGTH = 16
# Bits per character across the secret, as sampled: even 16 random hex characters stay above this;
# a word repeated (caught below too) or a handful of distinct characters falls under it.
MIN_SECRET_ENTROPY_BITS = 2.5
# Passwords people pick. Long enough that a generated secret practically never contains one.
_COMMON_WORDS = ("password", "passw0rd", "qwerty", "letmein", "iloveyou", "changeme")


def _entropy_bits_per_character(text: str) -> float:
    counts = Counter(text)
    return -sum((n / len(text)) * math.log2(n / len(text)) for n in counts.values())


def _repeats_a_pattern(text: str) -> bool:
    """Whole-unit repetition, or repeated words/runs with changing separators or suffixes.

    For word repetition, numbers and separators are ignored and the remaining letters must be a
    unit repeated end to end. Two things then tell a person's pattern from chance: the unit
    repeats at least four times (``ab1ab2ab3ab4...``, ``k-1k-2k-3...``), or letters make up at
    least half of the value (``Parola1Parola2``). A random hex token is mostly digits, so its
    few letters can line up twice or three times by accident (``adddc`` + ``adddc``); that is
    not a repeated word. Exact repetition and dominant repeated runs are checked regardless.
    """
    text = text.casefold()
    period = (text + text).find(text, 1)
    if period < len(text):
        return True
    # Changing numeric suffixes must not disguise a word repeated throughout a credential.
    letters = re.sub(r"[\W\d_]+", "", text)
    if len(letters) >= 9:
        letter_period = (letters + letters).find(letters, 1)
        if letter_period < len(letters) and (
            len(letters) // letter_period >= 4 or len(letters) * 2 >= len(text)
        ):
            return True
    runs = Counter(re.findall(r"[^\W\d_]+|\d+|[\W_]+", text))
    return any(
        len(run) >= 3 and count >= 2 and len(run) * count >= len(text) * 0.6
        for run, count in runs.items()
    )


def _mostly_sequential(text: str) -> bool:
    """``abcdefgh...`` or ``87654321...``: runs of consecutive characters, not randomness."""
    steps = [ord(right) - ord(left) for left, right in pairwise(text)]
    return max(steps.count(1), steps.count(-1)) >= len(steps) / 2


def _credential(value: str) -> str:
    """A scheme does not make a credential stronger: ``Bearer``/``Basic`` are ignored."""
    return re.sub(r"^(?:Bearer|Basic)\s+", "", value.strip(), flags=re.IGNORECASE)


def legacy_strong_secret(value: str) -> bool:
    """The rule before generated strength was required. Only ever says yes or no."""
    credential = _credential(value)
    return (
        len(credential) >= MIN_LEGACY_SECRET_LENGTH
        and _entropy_bits_per_character(credential) >= MIN_SECRET_ENTROPY_BITS
        and not _repeats_a_pattern(credential)
        and not _mostly_sequential(credential)
    )


def strong_secret(value: str) -> bool:
    """Whether a secret has the strength of a generated one; only ever says yes or no.

    Length is what separates a generated token from a password that merely looks mixed:
    ``Password1234567!`` has the characters but not the length. Callers must never log the value.
    """
    credential = _credential(value)
    folded = credential.casefold()
    return (
        len(credential) >= MIN_GENERATED_SECRET_LENGTH
        and legacy_strong_secret(credential)
        and not any(word in folded for word in _COMMON_WORDS)
    )
