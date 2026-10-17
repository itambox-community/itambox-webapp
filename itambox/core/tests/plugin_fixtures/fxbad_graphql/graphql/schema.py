import strawberry


@strawberry.type
class Query:
    @strawberry.field
    def broken(self) -> "FixtureTypeThatDoesNotExist":  # noqa: F821
        return None
