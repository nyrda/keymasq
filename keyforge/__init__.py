try:
    from importlib import metadata

    __version__ = metadata.version("python-keyforge")
except Exception:
    __version__ = "0.1.0.dev"
