from urllib.parse import quote

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, resolve_url
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from rest_framework import exceptions
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from strawberry.django.views import GraphQLView
from strawberry.extensions import AddValidationRules, MaxAliasesLimiter, MaxTokensLimiter, QueryDepthLimiter

from core.context import set_current_user
from users.api.authentication import TokenAuthentication

# Breadth budget for a single operation document. Strawberry's own
# ``MaxAliasesLimiter`` covers alias amplification and ``QueryDepthLimiter``
# covers nesting; this bounds the overall document size, standing in for the
# previous 500-field-selection cap.
MAX_OPERATION_TOKENS = 2000
MAX_OPERATION_ALIASES = 50
MAX_QUERY_DEPTH = 10


def query_complexity_validator(max_complexity=1000, fan_out=10):
    """Bound the estimated execution cost of a single operation.

    Depth and field-count limits cap the *shape* of a query but not its *cost*:
    a query can stay shallow and within the field cap while still triggering
    O(N*M) database work by nesting *list*-returning fields (e.g.
    ``assets { components { manufacturer { softwareProducts { name } } } }``).
    Every list nesting multiplies the number of rows resolved, so a handful of
    selections can fan out into a denial-of-service.

    This rule accumulates a cost score while walking the query: each field
    instance costs the product of the fan-out factors of the list fields that
    enclose it. A list-returning field multiplies the cost of everything beneath
    it by ``fan_out``; a singular object relation (e.g. ``tenant``) does not,
    since it resolves a single row. A ``GraphQLError`` is reported when the
    running total exceeds ``max_complexity``.

    Fields reached through a *named fragment* are accounted for too: graphql-core
    visits each fragment definition only once at the document top level, so the
    main walk never descends into a ``...spread``. To stop a query from hiding
    fan-out inside fragments (``a0: assets { ...F } ... fragment F on Asset {
    <expensive list path> }``), :meth:`enter_fragment_spread` runs a sub-walk of
    the referenced fragment's selection set, seeded with the spread site's
    current multiplier and a ``TypeInfo`` rooted at the fragment's type
    condition, and folds the resulting cost back in at the spread site.

    Mirrors :func:`field_count_limit_validator`: a factory returning a
    ``ValidationRule`` subclass that uses the ``enter_field`` / ``leave_field``
    visitor hooks and reports via ``self.report_error(GraphQLError(...))``. The
    base ``ValidationRule`` runs under graphql-core's ``TypeInfo`` visitor, so
    ``self.context.get_type()`` yields the live output type used to detect lists.
    """
    from graphql import visit
    from graphql.error import GraphQLError
    from graphql.language.visitor import Visitor
    from graphql.type import GraphQLList, GraphQLNonNull
    from graphql.utilities import TypeInfo, TypeInfoVisitor, type_from_ast
    from graphql.validation import ValidationRule

    def _returns_list(output_type):
        """True if the field's output type is (a non-null wrapper around) a list."""
        if isinstance(output_type, GraphQLNonNull):
            output_type = output_type.of_type
        return isinstance(output_type, GraphQLList)

    class QueryComplexityValidator(ValidationRule):
        def __init__(self, context):
            super().__init__(context)
            self._cost = 0
            self._reported = False
            # Stack of cost-multipliers for the enclosing field path. The root
            # selection set has a multiplier of 1.
            self._multipliers = [1]
            # Fragment names on the current spread path, to avoid unbounded
            # recursion on cyclic fragment references (which other validation
            # rules reject, but we must not hang before they run).
            self._fragment_path = set()

        def _report_if_over(self, node):
            if self._cost > max_complexity and not self._reported:
                self._reported = True
                self.report_error(GraphQLError(f"Query exceeds the maximum complexity of {max_complexity}.", node))

        def enter_field(self, node, *_args):
            multiplier = self._multipliers[-1]
            # Each selected field instance costs its enclosing multiplier.
            self._cost += multiplier
            self._report_if_over(node)
            # A list-returning field amplifies everything nested beneath it.
            output_type = self.context.get_type()
            child_multiplier = multiplier
            if _returns_list(output_type):
                child_multiplier *= fan_out
            self._multipliers.append(child_multiplier)

        def leave_field(self, node, *_args):
            self._multipliers.pop()

        def enter_fragment_spread(self, node, *_args):
            # The main document walk does not expand named fragments, so charge
            # the referenced fragment's field cost here, at the spread site, using
            # the multiplier accumulated by the enclosing field path.
            name = node.name.value
            if name in self._fragment_path:
                return  # cyclic spread — bail out (other rules report the cycle)
            fragment = self.context.get_fragment(name)
            if fragment is None:
                return  # undefined fragment — other rules report it
            root_type = type_from_ast(self.context.schema, fragment.type_condition)
            if root_type is None:
                return  # unknown type condition — other rules report it

            multiplier = self._multipliers[-1]
            self._fragment_path.add(name)
            self._account_for_selection_set(fragment.selection_set, root_type, multiplier)
            self._fragment_path.discard(name)

        def _account_for_selection_set(self, selection_set, root_type, base_multiplier):
            """Walk ``selection_set`` (a fragment body) and fold its field cost
            into the running total, treating ``base_multiplier`` as the cost of a
            field selected directly on ``root_type``.

            A fresh ``TypeInfo`` seeded at ``root_type`` resolves list-vs-singular
            output types exactly as the main walk does, so nested list fan-out
            inside the fragment is multiplied identically. Nested fragment spreads
            recurse through the outer validator (shared cost/multiplier state),
            so multi-hop fragment chains are charged too.
            """
            outer = self

            class _FragmentCostVisitor(Visitor):
                def __init__(self):
                    super().__init__()
                    # Mirror the outer multiplier stack, rooted at the spread site.
                    self._stack = [base_multiplier]

                def enter_field(self, node, *_a):
                    multiplier = self._stack[-1]
                    outer._cost += multiplier
                    outer._report_if_over(node)
                    output_type = type_info.get_type()
                    child_multiplier = multiplier
                    if _returns_list(output_type):
                        child_multiplier *= fan_out
                    self._stack.append(child_multiplier)

                def leave_field(self, node, *_a):
                    self._stack.pop()

                def enter_fragment_spread(self, node, *_a):
                    # Delegate to the outer rule so the cycle guard and the
                    # spread-site multiplier (top of this visitor's stack) apply.
                    outer._multipliers.append(self._stack[-1])
                    try:
                        outer.enter_fragment_spread(node)
                    finally:
                        outer._multipliers.pop()

            type_info = TypeInfo(self.context.schema, initial_type=root_type)
            visit(selection_set, TypeInfoVisitor(type_info, _FragmentCostVisitor()))

    return QueryComplexityValidator


def _has_pathless_errors(response_data):
    """True when the payload carries an operation error raised before execution.

    Parse and validation failures (including the depth, alias, document-size and
    complexity budgets) carry no ``path``; resolver failures do. The Graphene
    view this replaces answered the former with HTTP 400.
    """
    entries = response_data if isinstance(response_data, list) else [response_data]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for error in entry.get("errors") or ():
            if not error.get("path"):
                return True
    return False


def schema_extensions():
    """Operation budgets applied to every request.

    Depth, alias and document size use Strawberry's built-in limiters; the
    estimated *cost* of nested list fan-out has no built-in equivalent, so the
    custom complexity rule is kept alongside them.
    """
    return [
        lambda: QueryDepthLimiter(max_depth=MAX_QUERY_DEPTH),
        lambda: MaxAliasesLimiter(max_alias_count=MAX_OPERATION_ALIASES),
        lambda: MaxTokensLimiter(max_token_count=MAX_OPERATION_TOKENS),
        lambda: AddValidationRules([query_complexity_validator(max_complexity=2000, fan_out=10)]),
    ]


@method_decorator(csrf_exempt, name="dispatch")
class PrivateGraphQLView(GraphQLView):
    """Token-authenticated GraphQL endpoint with the interactive GraphiQL shell."""

    # Schema introspection is available to authenticated users in every
    # environment, including production: the schema is not a secret and the
    # endpoint stays auth-gated, and GraphiQL needs introspection for its
    # docs explorer and autocompletion (NetBox follows the same model). The
    # depth/alias/document/complexity budgets protect the endpoint regardless.
    # GET authentication is enforced in dispatch(); the interactive shell
    # (templates/graphql/graphiql.html) stays available to authenticated users.
    graphql_ide = "graphiql"
    allow_queries_via_get = True

    def dispatch(self, request, *args, **kwargs):
        if request.method == "GET" and not request.user.is_authenticated:
            return redirect(f"{resolve_url(settings.LOGIN_URL)}?next={quote(request.get_full_path())}")
        elif request.method == "POST":
            if request.user.is_authenticated:
                from django.middleware.csrf import CsrfViewMiddleware

                csrf_reject = CsrfViewMiddleware(lambda r: None).process_view(request, None, (), {})
                if csrf_reject:
                    return csrf_reject

            if not request.user.is_authenticated:
                try:
                    auth_result = TokenAuthentication().authenticate(request)
                    if auth_result is not None:
                        user, token = auth_result
                        request.user = user
                        request.auth = token
                        set_current_user(user)
                    else:
                        return HttpResponse(_("Unauthorized"), status=401)
                except ImproperlyConfigured:
                    raise
                except exceptions.AuthenticationFailed:
                    # Return a generic message; the specific failure reason must not
                    # leak to the client (aligns with the broad-except branch below).
                    return JsonResponse({"errors": [{"message": str(_("Authentication failed"))}]}, status=401)
                except Exception:
                    return JsonResponse({"errors": [{"message": str(_("Authentication failed"))}]}, status=401)

        if self._executes_query(request):
            throttled = self._throttle_response(request)
            if throttled is not None:
                return throttled

        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def _executes_query(request):
        """True for every request that executes an operation (POST, or GET carrying a query).

        A bare GET without a ``query`` parameter only serves the GraphiQL shell,
        executes nothing and stays outside the shared request budget.
        """
        return request.method == "POST" or (request.method == "GET" and "query" in request.GET)

    def _throttle_response(self, request):
        """Apply the REST throttles (shared GET/POST budget); return a 429 response or None."""
        for throttle in (AnonRateThrottle(), UserRateThrottle()):
            if not throttle.allow_request(request, self):
                wait = throttle.wait()
                message = _("Request was throttled. Expected available in %(wait)s seconds.") % {"wait": wait}
                return JsonResponse({"errors": [{"message": str(message)}]}, status=429)
        return None

    def create_response(self, response_data, sub_response):
        # Keep the HTTP contract of the previous Graphene view: an operation
        # rejected before execution answers 400 and carries no ``data`` member.
        if _has_pathless_errors(response_data):
            for entry in response_data if isinstance(response_data, list) else [response_data]:
                if isinstance(entry, dict):
                    entry.pop("data", None)
            response = super().create_response(response_data, sub_response)
            response.status_code = 400
            return response
        return super().create_response(response_data, sub_response)
