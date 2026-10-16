"""Root GraphQL schema for the read-only API.

The per-app query types are merged into one root type; plugins may contribute
their own Strawberry ``Query`` type (queries only) through
``PluginConfig.graphql_schema``; core apps expose no GraphQL mutations (writes
are REST-only, #612).
"""

import strawberry

import assets.schema
import inventory.schema
import licenses.schema
import software.schema
import subscriptions.schema
from assets.graphql_specifications.scalars import SCALAR_OVERRIDES
from core.views.graphql import schema_extensions
from itambox.registry import registry

query_bases = [
    assets.schema.Query,
    software.schema.Query,
    licenses.schema.Query,
    inventory.schema.Query,
    subscriptions.schema.Query,
]

# Plugin Query types were validated and registered by the host during the
# compose phase (queries only; a plugin Mutation is rejected at startup).
query_bases.extend(query for _, query in registry.get_plugin_graphql())

# Strawberry merges the fields of every base type into one root type.
Query = strawberry.type(type("Query", tuple(query_bases), {}), name="Query")


def build_schema(*, extensions=None) -> strawberry.Schema:
    """Build the composed schema.

    ``extensions`` defaults to the endpoint operation budgets; callers that
    exercise resolution without the HTTP contract (reader tests, tooling) pass
    an explicit empty sequence.
    """
    return strawberry.Schema(
        query=Query,
        extensions=schema_extensions() if extensions is None else extensions,
        scalar_overrides=SCALAR_OVERRIDES,
    )


schema = build_schema()
