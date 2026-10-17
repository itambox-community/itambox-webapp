from itambox.api.viewsets import BaseViewSet


class NoQuerysetViewSet(BaseViewSet):
    """No queryset and no basename: the router refuses to mount it."""


viewsets = [("broken", NoQuerysetViewSet)]
