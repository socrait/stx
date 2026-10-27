class StxError(Exception):
    """A user-facing error with an already-printable message. Not for
    programming errors — those stay as the native exception."""
