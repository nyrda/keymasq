try:
    from importlib import metadata

    __version__ = metadata.version("python-keymasq")
except Exception:
    __version__ = "0.2.dev"
