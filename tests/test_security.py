import logging
import socket
import struct
from pathlib import Path

import pytest

from keymasq.common.security import (
    SecurityPolicyError,
    get_peer_credentials,
    load_security_policy,
    uid_allowed,
)


def test_load_security_policy_defaults_when_missing(tmp_path: Path) -> None:
    policy = load_security_policy(tmp_path / "missing-security.toml")

    assert policy.recording_unlock_required is True
    assert policy.macro_recording_time_limit == 10
    assert policy.macro_edit_requires_unlock is False
    assert policy.emergency_cancel_combo_enabled is True


def test_load_security_policy_rejects_malformed_toml(tmp_path: Path) -> None:
    policy_path = tmp_path / "security.toml"
    policy_path.write_text("[macro\n")

    with pytest.raises(SecurityPolicyError, match="Invalid security policy TOML"):
        load_security_policy(policy_path)


def test_get_peer_credentials_reads_socket_peercred() -> None:
    class _Socket:
        def getsockopt(self, level: int, optname: int, buflen: int) -> bytes:
            assert level == socket.SOL_SOCKET
            assert optname == socket.SO_PEERCRED
            assert buflen == struct.calcsize("3i")
            return struct.pack("3i", 42, 1000, 1001)

    credentials = get_peer_credentials(_Socket())

    assert credentials is not None
    assert credentials.pid == 42
    assert credentials.uid == 1000
    assert credentials.gid == 1001


def test_get_peer_credentials_logs_unexpected_socket_error(caplog) -> None:
    class _Socket:
        def getsockopt(self, _level: int, _optname: int, _buflen: int) -> bytes:
            raise RuntimeError("socket wrapper failed")

    caplog.set_level(logging.ERROR, logger="keymasq.common.security")

    assert get_peer_credentials(_Socket()) is None
    assert "Unexpected failure reading peer credentials" in caplog.text


def test_load_security_policy_macro_section(tmp_path: Path) -> None:
    policy_path = tmp_path / "security.toml"
    policy_path.write_text(
        "\n".join(
            [
                "[macro]",
                "exec_timeout_max_ms = 12000",
            ]
        )
    )

    policy = load_security_policy(policy_path)

    assert policy.macro_exec_timeout_max_ms == 12000


def test_load_security_policy_recording_guard_section(tmp_path: Path) -> None:
    policy_path = tmp_path / "security.toml"
    policy_path.write_text(
        "\n".join(
            [
                "[recording_guard]",
                "unlock_required = false",
                "macro_recording_time_limit = 0",
                "macro_edit_requires_unlock = true",
            ]
        )
    )

    policy = load_security_policy(policy_path)

    assert policy.recording_unlock_required is False
    assert policy.macro_recording_time_limit == 0
    assert policy.macro_edit_requires_unlock is True


@pytest.mark.parametrize("value", ["-1", "true", '"10"'])
def test_load_security_policy_rejects_invalid_recording_duration(
    tmp_path: Path,
    value: str,
) -> None:
    policy_path = tmp_path / "security.toml"
    policy_path.write_text(
        "\n".join(
            [
                "[recording_guard]",
                f"macro_recording_time_limit = {value}",
            ]
        )
    )

    with pytest.raises(SecurityPolicyError, match="macro_recording_time_limit"):
        load_security_policy(policy_path)


def test_load_security_policy_gui_section(tmp_path: Path) -> None:
    policy_path = tmp_path / "security.toml"
    policy_path.write_text(
        "\n".join(
            [
                "[gui]",
                "emergency_cancel_combo_enabled = false",
            ]
        )
    )

    policy = load_security_policy(policy_path)

    assert policy.emergency_cancel_combo_enabled is False


def test_uid_allowlist_optional_and_enforced(tmp_path: Path) -> None:
    default_policy = load_security_policy(tmp_path / "missing-security.toml")
    assert default_policy.daemon_allowed_uids == []
    assert default_policy.session_allowed_uids == []
    assert uid_allowed(1000, default_policy.daemon_allowed_uids)

    policy_path = tmp_path / "security.toml"
    policy_path.write_text(
        "\n".join(
            [
                'daemon_allowed_uids = [1000, "1001"]',
                "session_allowed_uids = [1000]",
            ]
        )
    )
    policy = load_security_policy(policy_path)

    assert policy.daemon_allowed_uids == [1000, 1001]
    assert policy.session_allowed_uids == [1000]
    assert uid_allowed(1000, policy.daemon_allowed_uids)
    assert not uid_allowed(2000, policy.daemon_allowed_uids)


@pytest.mark.parametrize(
    ("setting_name", "setting_value"),
    [
        ("daemon_allowed_uids", '[1000, "invalid"]'),
        ("session_allowed_uids", "[true]"),
        ("daemon_allowed_uids", '"1000"'),
    ],
)
def test_load_security_policy_rejects_malformed_uid_allowlists(
    tmp_path: Path,
    setting_name: str,
    setting_value: str,
) -> None:
    policy_path = tmp_path / "security.toml"
    policy_path.write_text(f"{setting_name} = {setting_value}\n")

    with pytest.raises(SecurityPolicyError, match=setting_name):
        load_security_policy(policy_path)
