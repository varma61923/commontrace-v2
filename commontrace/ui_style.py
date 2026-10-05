"""One packaged theme source for browser assets and server-rendered consoles."""
from importlib.resources import files

THEME_CSS = files("commontrace").joinpath("ui/tokens.css").read_text(encoding="utf-8")
