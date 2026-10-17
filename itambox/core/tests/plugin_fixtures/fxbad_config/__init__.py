from fixture_support import current_range

from itambox.plugins import PluginConfig


class Config(PluginConfig):
    name = "fxbad_config"
    description = "Requires a setting the operator never provided."
    itambox_range = current_range()
    required_settings = ["api_token"]


config = Config
