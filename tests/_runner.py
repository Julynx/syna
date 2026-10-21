"""Shared harness for the project's plain-script test suites.

The suites are runnable with the project venv and no pytest dependency;
this module provides the patching primitive the tests use and a uniform
runner that executes every test, reports failures, and exits non-zero
when any test fails.
"""

import inspect
import sys
import traceback

_MISSING = object()


class MonkeyPatch:
    """Temporarily override attributes and mapping items; undo() restores.

    Every setattr/setitem is recorded so test teardown is automatic and
    ordering-independent, mirroring pytest's fixture of the same name.
    """

    def __init__(self):
        self._undo_stack = []

    def setattr(self, target, name, value):
        """Override target.name and remember the previous value."""
        previous = getattr(target, name, _MISSING)
        setattr(target, name, value)
        self._undo_stack.append(("attr", target, name, previous))

    def setitem(self, mapping, key, value):
        """Override mapping[key] and remember the previous state."""
        previous = mapping[key] if key in mapping else _MISSING
        mapping[key] = value
        self._undo_stack.append(("item", mapping, key, previous))

    def undo(self):
        """Restore every overridden attribute and item, last applied first."""
        for kind, target, key, previous in reversed(self._undo_stack):
            if previous is _MISSING:
                if kind == "attr":
                    delattr(target, key)
                else:
                    del target[key]
            elif kind == "attr":
                setattr(target, key, previous)
            else:
                target[key] = previous
        self._undo_stack.clear()


def run_suite(tests):
    """Run all test functions; exit non-zero when any of them fails.

    Tests declaring a `monkeypatch` parameter receive a fresh patcher
    that is undone after the test, whether it passed or failed.
    """
    failed = []
    for test in tests:
        patch = MonkeyPatch()
        try:
            if "monkeypatch" in inspect.signature(test).parameters:
                test(patch)
            else:
                test()
            print(f"PASS {test.__name__}")
        except Exception:
            failed.append(test.__name__)
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
        finally:
            patch.undo()
    if failed:
        print(f"{len(failed)} TEST(S) FAILED: {', '.join(failed)}")
        sys.exit(1)
    print(f"ALL {len(tests)} TESTS PASSED")
