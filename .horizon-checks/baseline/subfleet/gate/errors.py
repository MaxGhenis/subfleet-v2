"""Stable, user-facing gate failures (C-17.1)."""


class GateError(ValueError):
    """A gate failure carrying v1's process exit code."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code
