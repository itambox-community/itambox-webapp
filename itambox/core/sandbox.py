"""Hardened Jinja2 sandbox shared by every stored, user-authored template."""

import ctypes
import re
import threading
from contextlib import contextmanager

UNSAFE_FILTERS = ("attr", "format", "format_map", "map", "pprint", "xmlattr")
UNSAFE_GLOBALS = ("cycler", "joiner", "namespace", "lipsum")

# Operand budgets for operators and string methods that allocate in one C call and
# therefore cannot be interrupted by a wall-clock deadline once started.
MAX_SEQUENCE_ITEMS = 100_000
MAX_POWER_BITS = 100_000
MAX_PAD_WIDTH = 10_000
_BIG_FORMAT_NUMBER = re.compile(r"%[#0\- +]*\d{5,}|\.\d{5,}|%[#0\- +]*\*")
_PADDING_METHODS = frozenset({"ljust", "rjust", "center", "zfill", "expandtabs"})
_PADDING_FILTERS = ("center", "indent")


def hardened_sandbox_environment(autoescape=False):
    """An ``ImmutableSandboxedEnvironment`` without the documented SSTI primitives.

    ``ImmutableSandboxedEnvironment`` blocks dunder access and builtin mutation
    gadgets; the filters ``attr``/``format``/``format_map``/``map``/``pprint``/
    ``xmlattr`` and the globals ``cycler``/``joiner``/``namespace``/``lipsum`` are
    removed on top.  Environment parameters (``finalize``, ``undefined``, ...) are
    never exposed to stored template data.  Repetition (``*``), exponent (``**``),
    ``%`` formatting and padding operands are budgeted so one expression cannot
    allocate an arbitrarily large value; the environment exposes
    ``sandbox_limit_error`` for callers that translate a budget violation.
    """
    # inline import: heavy-import: jinja2 is a render-only dependency; keep it off the
    # import-time path of modules that merely reference this helper.
    from jinja2.exceptions import SecurityError
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    class SandboxLimitError(SecurityError):
        """An operand exceeded the sandbox allocation budget."""

    class BudgetedSandboxEnvironment(ImmutableSandboxedEnvironment):
        intercepted_binops = frozenset({"*", "**", "%"})

        def call_binop(self, context, operator, left, right):
            _check_binop(operator, left, right, SandboxLimitError)
            return super().call_binop(context, operator, left, right)

        def call(self, context, obj, *args, **kwargs):
            owner = getattr(obj, "__self__", None)
            if isinstance(owner, (str, bytes)) and getattr(obj, "__name__", "") in _PADDING_METHODS:
                _check_widths(args, SandboxLimitError)
            return super().call(context, obj, *args, **kwargs)

    env = BudgetedSandboxEnvironment(autoescape=autoescape)
    env.sandbox_limit_error = SandboxLimitError
    for unsafe_filter in UNSAFE_FILTERS:
        env.filters.pop(unsafe_filter, None)
    for unsafe_global in UNSAFE_GLOBALS:
        env.globals.pop(unsafe_global, None)
    for padded in _PADDING_FILTERS:
        env.filters[padded] = _limited_filter(env.filters[padded], SandboxLimitError)
    return env


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _repeats_too_much(seq, count):
    return (
        isinstance(seq, (str, bytes, list, tuple)) and _is_int(count) and len(seq) * max(count, 0) > MAX_SEQUENCE_ITEMS
    )


def _check_binop(operator, left, right, error):
    if operator == "*":
        if _repeats_too_much(left, right) or _repeats_too_much(right, left):
            raise error("Repetition result exceeds the template size budget")
    elif operator == "**":
        if _is_int(left) and _is_int(right) and right > 0 and max(abs(left).bit_length(), 1) * right > MAX_POWER_BITS:
            raise error("Exponent result exceeds the template size budget")
    elif operator == "%" and isinstance(left, (str, bytes)):
        text = left if isinstance(left, str) else left.decode("latin-1")
        if _BIG_FORMAT_NUMBER.search(text):
            raise error("Format width exceeds the template size budget")


def _check_widths(args, error):
    if args and _is_int(args[0]) and args[0] > MAX_PAD_WIDTH:
        raise error("Padding width exceeds the template size budget")


def _limited_filter(original, error):
    def limited(value, width=None, *args, **kwargs):
        if _is_int(width) and width > MAX_PAD_WIDTH:
            raise error("Padding width exceeds the template size budget")
        return original(value) if width is None else original(value, width, *args, **kwargs)

    limited.__name__ = getattr(original, "__name__", "limited")
    return limited


class RenderDeadlineExceeded(Exception):
    """The wall-clock budget of a render elapsed."""


@contextmanager
def render_deadline(seconds):
    """Interrupt the calling thread with ``RenderDeadlineExceeded`` after ``seconds``.

    The interrupt is delivered between Python bytecodes, so it stops template
    loops and any other pure-Python compute; it cannot interrupt a single C call
    (hence the operand budgets above).  Works in any thread and needs no signals.
    """
    thread_id = threading.get_ident()
    lock = threading.Lock()
    state = {"done": False}

    def fire():
        with lock:
            if not state["done"]:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_ulong(thread_id), ctypes.py_object(RenderDeadlineExceeded)
                )

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        with lock:
            state["done"] = True
        timer.cancel()
