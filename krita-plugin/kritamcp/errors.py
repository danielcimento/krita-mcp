"""Command errors (D2). Pure: no krita / PyQt5 imports."""

# Error code -> HTTP status (decisions.md D2).
HTTP_STATUS = {
    "bad_request": 400,
    "forbidden_path": 403,
    "not_found": 404,
    "ambiguous": 409,
    "conflict": 409,
    "too_large": 413,
    "krita_error": 500,
    "timeout": 504,
}


class CommandError(Exception):
    """A command failed in a way the caller should see.

    ``CommandError(code, message, details=None)``, with ``code`` one of
    ``HTTP_STATUS``.
    """

    def __init__(self, code, message, details=None):
        if code not in HTTP_STATUS:
            raise ValueError(f"unknown error code: {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    @property
    def http_status(self):
        return HTTP_STATUS[self.code]

    def to_dict(self):
        """The ``error`` member of the response envelope."""
        err = {"code": self.code, "message": self.message}
        if self.details is not None:
            err["details"] = self.details
        return err
