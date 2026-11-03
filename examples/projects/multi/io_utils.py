"""Small IO helpers (standard library only, no import-time side effects)."""


def read_lines(path):
    """Return the lines of the file at *path* as a list of strings.

    Line endings are stripped and the original order is preserved; no line is
    truncated or dropped. If the file does not exist, an empty list is
    returned instead of raising ``FileNotFoundError``.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return [line.rstrip("\r\n") for line in handle.readlines()]
    except FileNotFoundError:
        return []
