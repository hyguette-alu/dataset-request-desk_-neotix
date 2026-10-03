"""Domain errors.

Services raise these; `app.main` registers handlers that turn them into HTTP
responses. Keeping them separate means the rules in app/services/ can be
tested directly, without going through the API, and do not import FastAPI.
"""


class DomainError(Exception):
    """Base class. `status_code` is what the API handler will return."""

    status_code = 400

    def __init__(self, message: str, **context):
        super().__init__(message)
        self.message = message
        self.context = context


class NotFound(DomainError):
    status_code = 404


class PermissionDenied(DomainError):
    """The caller's role or ownership does not allow this action."""

    status_code = 403


class InvalidTransition(DomainError):
    """The requested status change is not an edge in the workflow, or the
    request is not in a state where it can be made."""

    status_code = 409


class AssignmentRejected(DomainError):
    """An episode cannot be attached to this request."""

    status_code = 409
