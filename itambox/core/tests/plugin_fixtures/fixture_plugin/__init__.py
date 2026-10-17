from fixture_support import current_range

from itambox.plugins import PluginConfig


class FixturePluginConfig(PluginConfig):
    name = "fixture_plugin"
    verbose_name = "Fixture plugin"
    description = "Maintained fixture exercising every declarative extension point."
    version = "1.0.0"
    base_url = "fixture"
    itambox_range = current_range()
    permission_targets = None


config = FixturePluginConfig
