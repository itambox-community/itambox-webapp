from fixture_support import current_range

from itambox.plugins import PluginConfig


class Config(PluginConfig):
    name = "fxbad_compose"
    description = "Declares template extensions that are not a list."
    itambox_range = current_range()
    template_extensions = "builtins.object"


config = Config
