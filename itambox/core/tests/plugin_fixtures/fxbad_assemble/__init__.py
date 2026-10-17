from fixture_support import current_range

from itambox.plugins import PluginConfig


class Config(PluginConfig):
    name = "fxbad_assemble"
    description = "Registers a viewset the router cannot mount."
    itambox_range = current_range()


config = Config
