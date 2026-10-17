from fixture_support import current_range

from itambox.plugins import PluginConfig


class Config(PluginConfig):
    name = "fxbad_urlconf"
    description = "Ships a URLconf that fails on import."
    itambox_range = current_range()


config = Config
