from itambox.plugins import PluginConfig


class Config(PluginConfig):
    name = "fxskip_range"
    description = "Declares a range that can never match the running release."
    itambox_range = ">=0.0,<0.1"


config = Config
