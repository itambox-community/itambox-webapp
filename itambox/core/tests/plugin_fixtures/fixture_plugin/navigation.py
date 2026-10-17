from itambox.plugins.navigation import PluginNavigationItem, PluginNavigationMenu


class FixtureMenuItem(PluginNavigationItem):
    link = "plugins:fixture_plugin:index"
    link_text = "Fixture entry"


class FixtureStandaloneItem(PluginNavigationItem):
    link = "plugins:fixture_plugin:index"
    link_text = "Fixture standalone entry"


class FixtureGroup:
    label = "Fixture group"
    items = (FixtureMenuItem,)


class FixtureMenu(PluginNavigationMenu):
    label = "Fixture menu"
    groups = (FixtureGroup,)


menu = FixtureMenu
menu_items = [FixtureStandaloneItem]
