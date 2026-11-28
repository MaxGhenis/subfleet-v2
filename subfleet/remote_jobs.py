"""Local records for provider sessions started outside Subfleet (C-17.10)."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import ids, protocol
from .contracts import Exit
from .store import Store

_CLAUDE_PATH = re.compile(r"/code/(session_[A-Za-z0-9]+)")
# The hub assumed this Codex URL shape pending confirmation with a real task URL.
_CODEX_PATH = re.compile(r"/codex/tasks/(task_[A-Za-z0-9_]+)")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_URL_ERROR = "remote adopt: expected a Claude Code cloud session or Codex cloud task URL"
# The credential check also reads from token boundaries after hyphens and underscores.
_FIELD_LIMIT = 512


def parse_remote_url(url: str) -> tuple[str, str, str]:
    """Return the provider, provider id, and URL without a query or fragment."""
    if not isinstance(url, str):
        raise ValueError(_URL_ERROR)
    canonical = url.partition("#")[0].partition("?")[0]
    # urlsplit silently strips some control characters before parsing.
    if not canonical.startswith("https://") or any(
            char.isspace() or not char.isprintable() for char in canonical):
        raise ValueError(_URL_ERROR)
    try:
        parts = urlsplit(canonical)
    except ValueError:
        raise ValueError(_URL_ERROR) from None
    if parts.netloc == "claude.ai" and (match := _CLAUDE_PATH.fullmatch(parts.path)):
        return "claude-cloud", match[1], canonical
    if parts.netloc == "chatgpt.com" and (match := _CODEX_PATH.fullmatch(parts.path)):
        return "codex-cloud", match[1], canonical
    raise ValueError(_URL_ERROR)


def adopt(store: Store, args: protocol.RemoteAdoptArgs) -> dict:
    try:
        provider, provider_ref, url = parse_remote_url(args.url)
    except ValueError:
        raise protocol.ProtocolError(_URL_ERROR, reason="remote-url") from None
    if len(provider_ref) > _FIELD_LIMIT or ids.holds_credential(provider_ref):
        raise protocol.ProtocolError(_URL_ERROR, reason="remote-url")
    if isinstance(args.account, str) and "@" in args.account:
        raise protocol.ProtocolError("remote adopt: --account must be a label without an email address",
                                     reason="remote-account")
    if args.repo is not None and (not isinstance(args.repo, str) or not _REPO.fullmatch(args.repo)):
        raise protocol.ProtocolError("remote adopt: --repo must be OWNER/NAME", reason="remote-repo")
    for field in (args.repo, args.branch, args.name, args.account):
        if field is not None and (not isinstance(field, str) or len(field) > _FIELD_LIMIT
                                  or (field and not field.isprintable()) or "@" in field
                                  or ids.holds_credential(field)):
            raise protocol.ProtocolError(
                "remote adopt: metadata must be printable, at most 512 characters per field, "
                "and free of email addresses and credentials",
                reason="remote-metadata")
    row = store.adopt_remote_job(provider=provider, provider_ref=provider_ref, url=url, repo=args.repo,
                                 branch=args.branch, name=args.name, account=args.account)
    if row is None:
        raise protocol.ProtocolError("remote adopt: remote job is already adopted", Exit.REFUSED,
                                     "see subfleet remote list", reason="remote-duplicate")
    return {"remote_job": row}


def forget(store: Store, args: protocol.RemoteForgetArgs) -> dict:
    if not isinstance(args.remote_id, str) or not store.forget_remote_job(args.remote_id):
        raise protocol.ProtocolError("remote forget: unknown remote job", Exit.REFUSED,
                                     "see subfleet remote list", reason="remote-unknown")
    return {"remote_id": args.remote_id}
