from itambox.api.viewsets import ITAMBoxModelViewSet


class NoQuerysetViewSet(ITAMBoxModelViewSet):
    """No queryset and no basename: the router refuses to mount it."""


viewsets = [("broken", NoQuerysetViewSet)]
