"""Expected and unexpected failures from nested macro playback."""


class MacroCallError(RuntimeError):
    """A macro call cannot run because its configured target is invalid."""


class MacroChildPlaybackError(RuntimeError):
    """A child invocation failed unexpectedly, with its call chain attached."""
