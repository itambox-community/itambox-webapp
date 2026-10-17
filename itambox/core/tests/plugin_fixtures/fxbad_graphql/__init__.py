from fixture_support import current_range

from itambox.plugins import PluginConfig


class Config(PluginConfig):
    name = "fxbad_graphql"
    description = "Ships a GraphQL Query that imports but cannot build a schema."
    itambox_range = current_range()


config = Config
