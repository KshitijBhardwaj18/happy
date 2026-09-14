"""Turn raw incident evidence into a stable signature.

Pure functions, no I/O: strip the volatile parts of log lines (timestamps,
ids, addresses, numbers) so that the same underlying failure produces the
same signature even when pod names and times differ between occurrences.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter

from pydantic import BaseModel

# Tokens that mark a log line as error-ish and therefore worth fingerprinting.
ERROR_TOKENS: list[str] = [
    "error",
    "exception",
    "traceback",
    "fatal",
    "panic",
    "killed",
    "timeout",
    "refused",
    "oom",
]

_MIN_SIGNATURE_LEN = 8

# Normalization order matters: timestamps first, then uuids, ips, pod
# suffixes, hex, and finally plain integers. Running pod suffixes before the
# generic hex pattern keeps a pod's replicaset/pod-hash suffix from being
# chewed up piecemeal by the hex rule; running hex before integers means any
# run of 6+ hex-ish characters (including all-digit runs) collapses to
# <hex> before shorter leftover digit runs become <n>.
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b",
    re.IGNORECASE,
)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_POD_SUFFIX_RE = re.compile(r"-[a-z0-9]{5,10}-[a-z0-9]{5}\b", re.IGNORECASE)
_HEX_RE = re.compile(r"\b[0-9a-f]{6,}\b", re.IGNORECASE)
_INT_RE = re.compile(r"\b\d+\b")

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_line(line: str) -> str:
    """Strip volatile substrings from a log line and lowercase/collapse it."""
    text = line
    text = _TIMESTAMP_RE.sub("<ts>", text)
    text = _UUID_RE.sub("<uuid>", text)
    text = _IPV4_RE.sub("<ip>", text)
    text = _POD_SUFFIX_RE.sub("-<pod>", text)
    text = _HEX_RE.sub("<hex>", text)
    text = _INT_RE.sub("<n>", text)
    text = text.lower()
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _is_errorish(line: str) -> bool:
    lowered = line.lower()
    return any(token in lowered for token in ERROR_TOKENS)


def top_signatures(lines: list[str], k: int = 5) -> list[str]:
    """Normalize error-ish lines and return the top-k most frequent shapes."""
    counts: Counter[str] = Counter()
    for line in lines:
        if not _is_errorish(line):
            continue
        normalized = normalize_line(line)
        if len(normalized) < _MIN_SIGNATURE_LEN:
            continue
        counts[normalized] += 1
    return [signature for signature, _ in counts.most_common(k)]


class Fingerprint(BaseModel):
    id: str
    service: str
    namespace: str
    reason: str
    signatures: list[str]


def incident_fingerprint(
    service: str,
    namespace: str,
    reason: str,
    log_lines: list[str],
) -> Fingerprint:
    """Build a stable fingerprint for an incident from its raw log lines."""
    signatures = top_signatures(log_lines)
    basis = "|".join([service, namespace, reason, "|".join(sorted(signatures))])
    fingerprint_id = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]
    return Fingerprint(
        id=fingerprint_id,
        service=service,
        namespace=namespace,
        reason=reason,
        signatures=signatures,
    )
