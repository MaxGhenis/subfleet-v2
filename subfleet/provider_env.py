"""Provider launch environment, shared by lanes, recovery and guard preflight."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging
import os
import re
import ssl
import threading
from typing import Any

from .policy import codex_ca_bundle
from .sessions.transcripts import read_regular

CA_SOURCES = ("SSL_CERT_FILE", "SSL_CERT_DIR")


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
            data = read_regular(bundle)
            certificates = re.findall(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", data, re.S)
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
                                     bundle, exc)
            return result
        result["SSL_CERT_FILE"] = bundle
        return result

    def build(self, provider: str, env_add: Mapping[str, str] | None = None,
              env_remove: Iterable[str] = (), *,
              environ: Mapping[str, str] | None = None) -> dict[str, str]:
        """The environment passed to a guardian or a preflight subprocess."""
        environ = os.environ if environ is None else environ
        result = {**environ, **self.additions(provider, env_add, environ=environ)}
        for key in env_remove:
            result.pop(key, None)
        return result
