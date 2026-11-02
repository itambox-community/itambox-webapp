"""Hardened Jinja2 sandbox shared by every stored, user-authored template."""

UNSAFE_FILTERS = ("attr", "format", "format_map", "map", "pprint", "xmlattr")
UNSAFE_GLOBALS = ("cycler", "joiner", "namespace", "lipsum")


def hardened_sandbox_environment(autoescape=False):
    """An ``ImmutableSandboxedEnvironment`` without the documented SSTI primitives.

    ``ImmutableSandboxedEnvironment`` blocks dunder access and builtin mutation
    gadgets; the filters ``attr``/``format``/``format_map``/``map``/``pprint``/
    ``xmlattr`` and the globals ``cycler``/``joiner``/``namespace``/``lipsum`` are
    removed on top.  Environment parameters (``finalize``, ``undefined``, ...) are
    never exposed to stored template data.
    """
    # inline import: heavy-import: jinja2 is a render-only dependency; keep it off the
    # import-time path of modules that merely reference this helper.
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    env = ImmutableSandboxedEnvironment(autoescape=autoescape)
    for unsafe_filter in UNSAFE_FILTERS:
        env.filters.pop(unsafe_filter, None)
    for unsafe_global in UNSAFE_GLOBALS:
        env.globals.pop(unsafe_global, None)
    return env
