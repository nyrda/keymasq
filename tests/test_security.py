from pathlib import Path

from keymasq.common.security import command_allowed, load_security_policy, uid_allowed


def test_load_security_policy_defaults_when_missing(tmp_path: Path) -> None:
    policy = load_security_policy(tmp_path / "missing-security.toml")

    assert command_allowed("play_macro", policy.session_command_acl, "client")
    assert command_allowed("delete_macro", policy.session_command_acl, "client")
    assert command_allowed("unknown_command", policy.daemon_command_acl, "session")
    assert policy.recording_unlock_required is True
    assert policy.macro_edit_requires_unlock is False
    assert policy.gui_allow_left_right_click_remap is False
    assert policy.emergency_cancel_combo_enabled is True


def test_load_security_policy_overrides_acl(tmp_path: Path) -> None:
    policy_path = tmp_path / "security.toml"
    policy_path.write_text(
        "\n".join(
            [
                "[session_command_acl]",
                'client = ["!list_macros"]',
            ]
        )
    )

    policy = load_security_policy(policy_path)

    assert not command_allowed("list_macros", policy.session_command_acl, "client")
    assert command_allowed("play_macro", policy.session_command_acl, "client")


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
                "macro_edit_requires_unlock = true",
            ]
        )
    )

    policy = load_security_policy(policy_path)

    assert policy.recording_unlock_required is False
    assert policy.macro_edit_requires_unlock is True


def test_load_security_policy_gui_section(tmp_path: Path) -> None:
    policy_path = tmp_path / "security.toml"
    policy_path.write_text(
        "\n".join(
            [
                "[gui]",
                "allow_left_right_click_remap = true",
                "emergency_cancel_combo_enabled = false",
            ]
        )
    )

    policy = load_security_policy(policy_path)

    assert policy.gui_allow_left_right_click_remap is True
    assert policy.emergency_cancel_combo_enabled is False


def test_allowlist_entries_are_non_blocking(tmp_path: Path) -> None:
    policy_path = tmp_path / "security.toml"
    policy_path.write_text(
        "\n".join(
            [
                "[session_command_acl]",
                'client = ["get_status", "play_macro"]',
            ]
        )
    )

    policy = load_security_policy(policy_path)

    assert command_allowed("play_macro", policy.session_command_acl, "client")
    assert command_allowed("get_status", policy.session_command_acl, "client")
    assert command_allowed("list_profiles", policy.session_command_acl, "client")


def test_acl_deny_prefixes_are_supported(tmp_path: Path) -> None:
    policy_path = tmp_path / "security.toml"
    policy_path.write_text(
        "\n".join(
            [
                "[session_command_acl]",
                'client = ["!reload", "-delete_macro", "deny:play_macro"]',
            ]
        )
    )

    policy = load_security_policy(policy_path)

    assert not command_allowed("reload", policy.session_command_acl, "client")
    assert not command_allowed("delete_macro", policy.session_command_acl, "client")
    assert not command_allowed("play_macro", policy.session_command_acl, "client")
    assert command_allowed("get_status", policy.session_command_acl, "client")


def test_acl_explicit_wildcard_deny_blocks_all_commands(tmp_path: Path) -> None:
    policy_path = tmp_path / "security.toml"
    policy_path.write_text(
        "\n".join(
            [
                "[session_command_acl]",
                'client = ["!*"]',
            ]
        )
    )

    policy = load_security_policy(policy_path)

    assert not command_allowed("play_macro", policy.session_command_acl, "client")
    assert not command_allowed("get_active_profiles", policy.session_command_acl, "client")


def test_default_client_acl_allows_all_commands(tmp_path: Path) -> None:
    policy = load_security_policy(tmp_path / "missing-security.toml")

    assert command_allowed("list_macros", policy.session_command_acl, "client")
    assert command_allowed("play_macro", policy.session_command_acl, "client")
    assert command_allowed("cancel_macro_playback", policy.session_command_acl, "client")
    assert command_allowed("list_profiles", policy.session_command_acl, "client")
    assert command_allowed("enable_profile", policy.session_command_acl, "client")
    assert command_allowed("disable_profile", policy.session_command_acl, "client")
    assert command_allowed("toggle_profile", policy.session_command_acl, "client")
    assert command_allowed("get_macro", policy.session_command_acl, "client")
    assert command_allowed("create_macro", policy.session_command_acl, "client")
    assert command_allowed("update_macro", policy.session_command_acl, "client")
    assert command_allowed("rename_macro", policy.session_command_acl, "client")
    assert command_allowed("delete_macro", policy.session_command_acl, "client")
    assert command_allowed("unknown_command", policy.daemon_command_acl, "session")


def test_uid_allowlist_optional_and_enforced(tmp_path: Path) -> None:
    default_policy = load_security_policy(tmp_path / "missing-security.toml")
    assert default_policy.daemon_allowed_uids == []
    assert default_policy.session_allowed_uids == []
    assert uid_allowed(1000, default_policy.daemon_allowed_uids)

    policy_path = tmp_path / "security.toml"
    policy_path.write_text(
        "\n".join(
            [
                "daemon_allowed_uids = [1000, 1001]",
                "session_allowed_uids = [1000]",
            ]
        )
    )
    policy = load_security_policy(policy_path)

    assert policy.daemon_allowed_uids == [1000, 1001]
    assert policy.session_allowed_uids == [1000]
    assert uid_allowed(1000, policy.daemon_allowed_uids)
    assert not uid_allowed(2000, policy.daemon_allowed_uids)
