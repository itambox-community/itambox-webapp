import strawberry


@strawberry.type
class Query:
    @strawberry.field
    def fixture_plugin_ping(self) -> str:
        return "pong"
