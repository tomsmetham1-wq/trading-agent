"""
os_ca_bundle.py — bridge the OS (Windows) trust store to curl_cffi / libcurl.

yfinance ≥1.5 makes its HTTPS calls through curl_cffi (a libcurl binding), NOT
through Python's ssl module. The truststore.inject_into_ssl() fix in
trading_agent.py only patches Python's ssl layer, so it fixes the Anthropic SDK
and T212 (requests) calls but does nothing for curl_cffi. On a machine where
HTTPS is intercepted by a local TLS proxy — e.g. Avast/AVG "Web Shield",
corporate MITM, other AV HTTPS scanning — curl_cffi has no idea about the
interceptor's private root CA (which Windows itself already trusts), so every
yfinance price and FX call fails with:

    curl: (60) SSL certificate problem: unable to get local issuer certificate

libcurl reads a CA bundle from the CURL_CA_BUNDLE environment variable. This
module exports the Windows trust store to a PEM file and points CURL_CA_BUNDLE
at it. That trusts exactly what the OS already trusts — the same trust boundary
as the truststore fix — no verification is weakened.

No-op (and harmless) on non-Windows platforms and on networks with no
interceptor: the exported bundle is just the normal set of public roots, which
is what curl_cffi would have used anyway.
"""

from __future__ import annotations

import logging
import os
import ssl
from pathlib import Path

logger = logging.getLogger(__name__)

# Cached next to the other run artifacts; regenerated each process start so it
# tracks any root the OS added/removed. Gitignored — it's just public roots
# plus whatever local interceptor root the machine already trusts.
_BUNDLE_PATH = Path(os.getenv("OS_CA_BUNDLE_PATH", "os_ca_bundle.pem"))


def ensure_os_ca_bundle() -> None:
    """
    Export the Windows trust store to a PEM and set CURL_CA_BUNDLE so
    curl_cffi (and therefore yfinance) trusts the same roots the OS does.

    Idempotent and defensive: respects a CURL_CA_BUNDLE the user already set,
    silently no-ops on platforms without ssl.enum_certificates (i.e. non-
    Windows), and degrades to a warning rather than raising if anything fails —
    a broken bundle should never take down the whole run.
    """
    if os.environ.get("CURL_CA_BUNDLE"):
        return  # user/env already chose a bundle — don't override it
    if not hasattr(ssl, "enum_certificates"):
        return  # not Windows; certifi's default bundle is used as normal

    try:
        pem_parts: list[str] = []
        seen: set[bytes] = set()
        for store in ("ROOT", "CA"):
            for der, encoding, trust in ssl.enum_certificates(store):
                # trust is False only when the cert is explicitly DISTRUSTED;
                # True or a set of purpose OIDs both mean "usable".
                if encoding != "x509_asn" or trust is False:
                    continue
                if der in seen:
                    continue
                seen.add(der)
                pem_parts.append(ssl.DER_cert_to_PEM_cert(der))

        if not pem_parts:
            logger.warning("OS trust store returned no certs — leaving curl_cffi default")
            return

        _BUNDLE_PATH.write_text("".join(pem_parts), encoding="ascii")
        resolved = str(_BUNDLE_PATH.resolve())
        # curl_cffi / libcurl read CURL_CA_BUNDLE. Only this is set — Python's
        # own TLS is already handled by truststore, so we don't touch
        # SSL_CERT_FILE / REQUESTS_CA_BUNDLE and risk changing that path.
        os.environ["CURL_CA_BUNDLE"] = resolved
        logger.info("Exported %d OS root certs to %s for curl_cffi/yfinance",
                    len(pem_parts), resolved)
    except Exception as e:
        logger.warning(
            "Could not build OS CA bundle (yfinance may fail behind a TLS "
            "interceptor): %s", e,
        )
