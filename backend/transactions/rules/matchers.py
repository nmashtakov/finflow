import re


def normalize_match_value(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def match_equals(actual, expected: str) -> bool:
    return normalize_match_value(actual) == normalize_match_value(expected)


def match_contains(actual, expected: str) -> bool:
    haystack = normalize_match_value(actual)
    needle = normalize_match_value(expected)
    return bool(needle) and needle in haystack


def match_regex(actual, pattern: str) -> bool:
    text = normalize_match_value(actual)
    try:
        return re.search(pattern, text, flags=re.IGNORECASE) is not None
    except re.error:
        return False


def matches_condition(actual, operator: str, expected: str) -> bool:
    if operator == "equals":
        return match_equals(actual, expected)
    if operator == "contains":
        return match_contains(actual, expected)
    if operator == "regex":
        return match_regex(actual, expected)
    return False
