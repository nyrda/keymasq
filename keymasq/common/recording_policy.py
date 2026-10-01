from collections.abc import Mapping

from keymasq.common.paths import SECURITY_POLICY_PATH

MACRO_RECORDING_DISABLED_ERROR_CODE = "macro_recording_disabled"
MACRO_RECORDING_DISABLED_MESSAGE = (
    f"Macro recording is disabled by the administrator in {SECURITY_POLICY_PATH}."
)


def is_macro_recording_disabled_error(result: Mapping[str, object] | None) -> bool:
    if result is None:
        return False
    if result.get("error_code") == MACRO_RECORDING_DISABLED_ERROR_CODE:
        return True
    message = str(result.get("message", "") or "")
    return MACRO_RECORDING_DISABLED_ERROR_CODE in message
