from itambox.plugins import PluginConfig


class Config(PluginConfig):
    name = "fxbad_range_malformed"
    description = "Declares a malformed itambox_range."
    itambox_range = "1.x"


config = Config
