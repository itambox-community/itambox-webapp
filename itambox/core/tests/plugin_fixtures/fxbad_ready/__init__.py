from fixture_support import current_range

from itambox.plugins import PluginConfig


class Config(PluginConfig):
    name = "fxbad_ready"
    description = "Fails inside its own ready()."
    itambox_range = current_range()

    def ready(self):
        raise RuntimeError("fixture ready() failure")


config = Config
