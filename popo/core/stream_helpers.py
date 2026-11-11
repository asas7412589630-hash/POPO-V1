"""Stream plumbing kept apart from the pipeline body.

These three functions exist so ``pipeline.py`` stays inside the size budget; they
have no domain knowledge beyond "read, stop, and name what happened".
"""

from __future__ import annotations

from popo.errors import ProxyError


def _read_chunk(stream) -> bytes:
    """Return the next chunk from a stream handle or an iterator.

    ``UpstreamStream`` exposes ``read()`` so a caller can stop early without the
    generator ``finally`` closing the socket. Test doubles and alternative
    transports may only be iterators, so both shapes are accepted here — at the
    boundary, once, rather than scattered ``hasattr`` checks.
    """
    reader = getattr(stream, "read", None)
    if reader is not None:
        return reader()
    try:
        return next(stream)
    except StopIteration:
        return b""

def _abort(stream) -> None:
    """Stop an upstream stream without letting its failure surface as an error."""
    abort = getattr(stream, "abort", None)
    if abort is None:
        close = getattr(stream, "close", None)
        if close:
            try:
                close()
            except (OSError, ProxyError):
                pass
        return
    try:
        abort()
    except (OSError, ProxyError):
        pass

def _stream_verdict(decision: str, attempt_number: int) -> str:
    # Already committed: a refusal that could not be retried is a failure, and
    # calling it "stream" would hide that from the operator.
    if decision in ("refusal", "empty"):
        return "retry_failed"
    return "stream" if attempt_number == 1 else "retry_ok"
