"""Provider launch environment, shared by lanes, recovery and guard preflight."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging
import os
import ssl
import threading
from typing import Any

from . import refusals
from .policy import codex_ca_bundle
from .sessions.transcripts import read_regular

CA_SOURCES = ("SSL_CERT_FILE", "SSL_CERT_DIR")
# System bundles are a few hundred KiB. Allow ample room for custom roots
# while bounding launch-worker memory and validation work.
CA_BUNDLE_MAX_BYTES = 4 * 1024 * 1024
_PEM_BEGIN = b"-----BEGIN CERTIFICATE-----"
_PEM_END = b"-----END CERTIFICATE-----"


def _pem_certificates(data: bytes) -> list[bytes]:
    """Extract nonoverlapping PEM blocks with a forward-only, linear scan."""
    certificates = []
    cursor = 0
    while True:
        start = data.find(_PEM_BEGIN, cursor)
        if start < 0:
            break
        end = data.find(_PEM_END, start + len(_PEM_BEGIN))
        if end < 0:
            break
        cursor = end + len(_PEM_END)
        certificates.append(data[start:cursor])
    return certificates


class ProviderEnvironment:
    """C-10.8: Codex uses a PEM bundle unless the operator chose its CA source.

    A shared instance logs an unusable path once, including concurrent launches.
    Presence, even an empty value, is an operator choice. If the operator set
    both variables, both are preserved; Subfleet only ever adds one itself.
    """

    def __init__(self, policy: Mapping[str, Any], *, log: logging.Logger | None = None):
        self.policy = policy
        self.log = log if log is not None else logging.getLogger(__name__)
        self._warned: set[str] = set()
        self._warning_lock = threading.Lock()

    def additions(self, provider: str, env_add: Mapping[str, str] | None = None, *,
                  environ: Mapping[str, str] | None = None) -> dict[str, str]:
        """Build Launch.env_add without mutating the daemon's environment."""
        result = dict(env_add or {})
        if provider != "codex":
            return result
        environ = os.environ if environ is None else environ
        # Reconstructed launches use today's operator environment and policy.
        for key in CA_SOURCES:
            result.pop(key, None)
        operator = {key: environ[key] for key in CA_SOURCES if key in environ}
        if operator:
            result.update(operator)
            return result
        bundle = codex_ca_bundle(self.policy)
        if bundle is None:
            return result
        try:
            data = read_regular(bundle, limit=CA_BUNDLE_MAX_BYTES)
            certificates = _pem_certificates(data)
            if not certificates:
                raise ValueError("bundle contains no PEM certificates")
            # An empty SSLContext reads only these bytes, never native roots.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cadata=b"\n".join(certificates).decode("ascii"))
            if context.cert_store_stats()["x509"] < 1:
                raise ValueError("bundle contains no certificates")
        except (OSError, ValueError, UnicodeError) as exc:
            with self._warning_lock:
                if bundle not in self._warned:
                    self._warned.add(bundle)
                    self.log.warning("Codex CA bundle %s is unusable (%s); leaving SSL_CERT_FILE unset",
                                     bundle, refusals.scrubbed_words(exc))
            return result
        result["SSL_CERT_FILE"] = bundle
        return result

    def build(self, provider: str, env_add: Mapping[str, str] | None = None,
              env_remove: Iterable[str] = (), *,
              environ: Mapping[str, str] | None = None) -> dict[str, str]:
        """The environment passed to a guardian or a preflight subprocess."""
        environ = os.environ if environ is None else environ
        return self.assemble(self.additions(provider, env_add, environ=environ),
                             env_remove, environ=environ)

    @staticmethod
    def assemble(env_add: Mapping[str, str] | None = None,
                 env_remove: Iterable[str] = (), *,
                 environ: Mapping[str, str] | None = None) -> dict[str, str]:
        """Merge already-selected additions without selecting or validating again.

        A launch selects with ``additions`` once, then uses that result for
        adapter inspection, preflight and its guardian. Recovery selects anew.
        """
        environ = os.environ if environ is None else environ
        result = {**environ, **(env_add or {})}
        for key in env_remove:
            result.pop(key, None)
        return result
