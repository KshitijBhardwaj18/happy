from fingerprint import ERROR_TOKENS, Fingerprint, incident_fingerprint, normalize_line, top_signatures


def test_normalize_line_strips_timestamp_uuid_ip_hex_and_numbers():
    line = (
        "2026-09-14T03:12:01Z ERROR checkout: connection to 10.0.1.23 refused "
        "(req a1b2c3d4-e5f6-7890-abcd-ef0123456789, retry 3, hex deadbeefcafe)"
    )
    normalized = normalize_line(line)
    assert "<ts>" in normalized
    assert "<uuid>" in normalized
    assert "<ip>" in normalized
    assert "<hex>" in normalized
    assert "<n>" in normalized
    assert "2026" not in normalized
    assert "10.0.1.23" not in normalized


def test_normalize_line_strips_pod_suffix():
    line = "OOMKilled pod checkout-7d9f8b6c5d-x2vqp exceeded memory limit"
    normalized = normalize_line(line)
    assert "<pod>" in normalized
    assert "x2vqp" not in normalized
    assert "checkout" in normalized


def test_normalize_line_lowercases_and_collapses_whitespace():
    normalized = normalize_line("  ERROR   Something    Bad   Happened  ")
    assert normalized == "error something bad happened"


def test_top_signatures_ignores_short_lines_and_non_error_lines():
    lines = [
        "2026-09-14T03:12:00Z INFO checkout: handled request",
        "2026-09-14T03:12:01Z ERROR checkout: oom",  # too short after normalization
        "2026-09-14T03:12:02Z ERROR checkout: payment gateway timeout order=42",
        "2026-09-14T03:12:03Z ERROR checkout: payment gateway timeout order=99",
    ]
    signatures = top_signatures(lines)
    assert any("payment gateway timeout" in sig for sig in signatures)
    assert all(len(sig) >= 8 for sig in signatures)


def test_top_signatures_returns_top_k_by_frequency():
    lines = (
        ["2026-01-01T00:00:00Z ERROR checkout: frequent failure happened"] * 3
        + ["2026-01-01T00:00:00Z ERROR checkout: rare failure happened"] * 1
    )
    signatures = top_signatures(lines, k=1)
    assert len(signatures) == 1
    assert "frequent failure" in signatures[0]


def test_error_tokens_exported():
    assert "oom" in ERROR_TOKENS
    assert "timeout" in ERROR_TOKENS


def _oom_lines(pod_suffix: str, ts: str) -> list[str]:
    return [
        f"{ts} ERROR checkout: pod checkout-{pod_suffix} killed, reason OOMKilled",
        f"{ts} WARN checkout: memory usage exceeded limit before kill",
    ]


def test_fingerprint_stable_across_pod_names_and_timestamps():
    fp1 = incident_fingerprint(
        "checkout", "shop", "OOMKilled", _oom_lines("7d9f8b6c5d-x2vqp", "2026-09-14T03:12:01Z")
    )
    fp2 = incident_fingerprint(
        "checkout", "shop", "OOMKilled", _oom_lines("9a1b2c3d4e-q7wte", "2026-09-15T11:45:59Z")
    )
    assert isinstance(fp1, Fingerprint)
    assert fp1.id == fp2.id
    assert fp1.signatures == fp2.signatures


def test_fingerprint_differs_for_different_reason():
    oom_fp = incident_fingerprint(
        "checkout", "shop", "OOMKilled", _oom_lines("7d9f8b6c5d-x2vqp", "2026-09-14T03:12:01Z")
    )
    crashloop_lines = [
        "2026-09-14T03:12:01Z FATAL inventory: panic: nil pointer dereference in handler",
        "2026-09-14T03:12:02Z ERROR inventory: CrashLoopBackOff restarting container",
    ]
    crash_fp = incident_fingerprint("inventory", "shop", "CrashLoopBackOff", crashloop_lines)
    assert oom_fp.id != crash_fp.id
