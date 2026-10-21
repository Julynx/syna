"""Unit tests for the web_interact screenshot command."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.async_api import Error as PWError

from syna import tools
from syna.state import pop_last_exit_code

FAKE_PNG = b"\x89PNG\r\n\x1a\nfake-png-data"


class FakeContainer:
    """Records exec and copy_to calls instead of touching a real pod."""

    def __init__(self):
        self.exec_calls = []
        self.copied = []

    def exec(self, argv):
        self.exec_calls.append(argv)
        return ""

    def copy_to(self, source, destination):
        self.copied.append((source, destination))


def test_screenshot_saves_image_into_pod(monkeypatch):
    container = FakeContainer()
    monkeypatch.setattr(tools, "get_container", lambda: container)
    monkeypatch.setattr(tools, "BROWSER", object())
    monkeypatch.setitem(tools.BROWSER_COMMANDS, "screenshot", lambda browser: FAKE_PNG)

    result = tools.web_interact("screenshot", {})

    assert result.startswith("Screenshot saved to /workspace/screenshots/")
    assert "describe_image" in result
    assert "capped to the viewport" in result
    assert any(
        "mkdir -p /workspace/screenshots" in " ".join(argv)
        for argv in container.exec_calls
    )
    assert len(container.copied) == 1
    source, destination = container.copied[0]
    assert destination == "/workspace/screenshots"
    assert Path(source).read_bytes() == FAKE_PNG
    assert Path(source).suffix == ".png"


def test_screenshot_filenames_are_unique(monkeypatch):
    container = FakeContainer()
    monkeypatch.setattr(tools, "get_container", lambda: container)
    monkeypatch.setattr(tools, "BROWSER", object())
    monkeypatch.setitem(tools.BROWSER_COMMANDS, "screenshot", lambda browser: FAKE_PNG)

    first = tools.web_interact("screenshot", {})
    second = tools.web_interact("screenshot", {})

    first_name = Path(first.split(" ")[3]).name
    second_name = Path(second.split(" ")[3]).name
    assert first_name != second_name


def test_browser_errors_follow_the_error_convention(monkeypatch):
    container = FakeContainer()
    monkeypatch.setattr(tools, "get_container", lambda: container)
    monkeypatch.setattr(tools, "BROWSER", object())

    def raise_pw_error(browser):
        raise PWError("page crashed mid-capture")

    monkeypatch.setitem(tools.BROWSER_COMMANDS, "screenshot", raise_pw_error)

    result = tools.web_interact("screenshot", {})

    assert result.startswith("ERROR:")
    assert pop_last_exit_code() == 1
    assert container.copied == []


def test_bad_arguments_follow_the_error_convention(monkeypatch):
    container = FakeContainer()
    monkeypatch.setattr(tools, "get_container", lambda: container)
    monkeypatch.setattr(tools, "BROWSER", object())

    def reject_args(browser, **kwargs):
        raise TypeError("unexpected keyword argument 'format'")

    monkeypatch.setitem(tools.BROWSER_COMMANDS, "screenshot", reject_args)

    result = tools.web_interact("screenshot", {"format": "png"})

    assert result.startswith("ERROR: bad arguments for screenshot")
    assert pop_last_exit_code() == 1


def test_screenshot_without_container_raises(monkeypatch):
    """Without a pod the call must fail loudly, not silently lose the image."""
    monkeypatch.setattr(tools, "get_container", lambda: None)
    monkeypatch.setattr(tools, "BROWSER", object())
    monkeypatch.setitem(tools.BROWSER_COMMANDS, "screenshot", lambda browser: FAKE_PNG)

    try:
        tools.web_interact("screenshot", {})
    except AttributeError:
        pass  # None.exec raises; parse.py reports it as a failed call
    else:
        raise AssertionError("expected an exception without a container")
