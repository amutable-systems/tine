"""Compare versions in the format shared by Linux image specifications."""

_DIGITS = frozenset("0123456789")
_LETTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_VALID = _DIGITS | _LETTERS | frozenset("~-.^")


def _skip_separators(value: str, offset: int) -> int:
    while offset < len(value) and value[offset] not in _VALID:
        offset += 1
    return offset


def _prefix_end(value: str, offset: int, alphabet: frozenset[str]) -> int:
    while offset < len(value) and value[offset] in alphabet:
        offset += 1
    return offset


def compare(left: str, right: str) -> int:
    """Compare two UAPI version strings, returning -1, 0, or 1."""
    left_at = right_at = 0

    while True:
        left_at = _skip_separators(left, left_at)
        right_at = _skip_separators(right, right_at)

        left_tilde = left_at < len(left) and left[left_at] == "~"
        right_tilde = right_at < len(right) and right[right_at] == "~"
        if left_tilde != right_tilde:
            return -1 if left_tilde else 1
        if left_tilde:
            left_at += 1
            right_at += 1

        left_done = left_at == len(left)
        right_done = right_at == len(right)
        if left_done or right_done:
            return (right_done > left_done) - (left_done > right_done)

        # Their order is significant: each marker sorts below anything considered after it.
        for marker in "-^.":
            left_has_marker = left_at < len(left) and left[left_at] == marker
            right_has_marker = right_at < len(right) and right[right_at] == marker
            if left_has_marker != right_has_marker:
                return -1 if left_has_marker else 1
            if left_has_marker:
                left_at += 1
                right_at += 1

        left_digits_end = _prefix_end(left, left_at, _DIGITS)
        right_digits_end = _prefix_end(right, right_at, _DIGITS)
        left_has_digits = left_digits_end != left_at
        right_has_digits = right_digits_end != right_at
        if left_has_digits or right_has_digits:
            if left_has_digits != right_has_digits:
                return -1 if not left_has_digits else 1

            left_significant = left_at
            while left_significant < left_digits_end and left[left_significant] == "0":
                left_significant += 1
            right_significant = right_at
            while right_significant < right_digits_end and right[right_significant] == "0":
                right_significant += 1

            left_length = left_digits_end - left_significant
            right_length = right_digits_end - right_significant
            if left_length != right_length:
                return -1 if left_length < right_length else 1

            left_number = left[left_significant:left_digits_end]
            right_number = right[right_significant:right_digits_end]
            if left_number != right_number:
                return -1 if left_number < right_number else 1

            left_at = left_digits_end
            right_at = right_digits_end
            continue

        left_letters_end = _prefix_end(left, left_at, _LETTERS)
        right_letters_end = _prefix_end(right, right_at, _LETTERS)
        left_letters = left[left_at:left_letters_end]
        right_letters = right[right_at:right_letters_end]
        if left_letters != right_letters:
            return -1 if left_letters < right_letters else 1

        left_at = left_letters_end
        right_at = right_letters_end
