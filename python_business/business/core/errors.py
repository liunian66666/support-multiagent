"""HTTP and body-code errors; audit depends on their distinct semantics."""

from fastapi import HTTPException


def deny(code, message):
    raise HTTPException(status_code=code, detail=message)


def code_to_status(code):
    """Map the legacy body code to its externally visible HTTP status."""
    if code >= 500:
        return 500
    if code >= 400:
        return code if code in (400, 401, 403, 404, 405, 409, 429) else 400
    return 200


class SoftError(Exception):
    """Business failure that still qualifies for write auditing."""

    def __init__(self, code, message):
        self.code, self.message = code, message


def soft(code, message):
    raise SoftError(code, message)
