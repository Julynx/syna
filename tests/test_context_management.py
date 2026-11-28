"""Tests for token estimation, tool-output elision, and pod resource limits."""

import itertools
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna.protocol.channel import (
    format_tool_result_content,
    is_channel_message,
    unwrap_channel,
)
from syna.protocol.context import (
    elide_images,
    elide_tool_outputs,
    estimate_history_tokens,
    estimate_tokens,
    recent_window_size,
)
from syna.state import build_pod_host_config, register_container, unregister_container
from syna.capabilities.tools import TOOL_REGISTRY


class FakePod:
    """Records exec and copy_to calls, standing in for a bollard Container."""

    def exec(self, argv, **kwargs):
        return ""

    def copy_to(self, host_path: str, pod_dir: str):
        Path(host_path).write_text("persisted", encoding="utf-8")


def _tool_results_message(
    call: int, tool: str, output: str, saved_to: str | None
) -> dict:
    content = format_tool_result_content(status="ok", output=output, saved_to=saved_to)
    return {"role": "tool", "tool_call_id": f"call_{call}", "content": content}


def _image_message(path: str, image_tokens: int) -> dict:
    """Mirror `_image_history_message`'s shape: list content plus estimates."""
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": f"inspect_image attached '{path}':"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
        ],
        "image_tokens": image_tokens,
        "image_path": path,
    }


def test_estimate_tokens_bounds():
    assert estimate_tokens("") >= 1
    assert estimate_tokens("x" * 400) == 100
    assert estimate_tokens("x" * 401) == 101


def test_estimate_tokens_is_script_aware():
    cjk = "这是用于测试估算器的一段中文文本，包含标点和数字12345。"
    latin = "The quick brown fox jumps over the lazy dog near the riverbank."
    per_char_cjk = estimate_tokens(cjk) / len(cjk)
    per_char_latin = estimate_tokens(latin) / len(latin)
    assert per_char_cjk > per_char_latin * 2.2, (
        "CJK text must score far more tokens per character than latin text;"
        " a single global ratio underestimates CJK by ~2.5x"
    )


def test_estimate_tokens_tracks_real_tokenizer_counts():
    """Bounds measured once against the o200k tokenizer (offline install).

    The estimator must stay within 25% of the real count on these
    samples; the hard-coded numbers keep the test offline.
    """
    samples = [
        ("The quick brown fox jumps over the lazy dog near the riverbank.", 14),
        ("这是用于测试估算器的一段中文文本，包含标点和数字12345。", 19),
        ("これはトークン推定をテストするための日本語のテキストです。", 20),
        ("이것은 토큰 추정을 테스트하기 위한 한국어 텍스트입니다.", 17),
        ("Chart 图表 generated for the user, 標題 included.", 13),
    ]
    for text, real_tokens in samples:
        estimate = estimate_tokens(text)
        assert abs(estimate - real_tokens) <= real_tokens * 0.25, (
            f"estimate drifted from the real tokenizer: {text[:20]!r}"
            f" estimated {estimate}, real {real_tokens}"
        )


def test_estimate_history_tokens_counts_every_message():
    messages = [
        {"role": "system", "content": "x" * 400},
        {"role": "user", "content": "y" * 40},
    ]
    total = estimate_history_tokens(messages)
    assert total == 100 + 10 + 2 * 4


def test_elide_replaces_old_saved_outputs_with_pointers():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        _tool_results_message(
            1,
            "web_search",
            "long result body " + "p" * 200,
            "/tool_outputs/0001_web_search.txt",
        ),
    ]
    elided = elide_tool_outputs(messages, keep_recent=0)
    assert elided == 1
    _kind, body = unwrap_channel(messages[2]["content"])
    assert "[output elided; full text in /tool_outputs/0001_web_search.txt]" in body
    assert "long result body" not in body
    assert '<result status="ok"' in body
    assert is_channel_message(messages[2]["content"], "TOOL_RESULTS")


def test_elide_keeps_results_without_saved_to():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        _tool_results_message(1, "execute_command", "precious output", None),
    ]
    elided = elide_tool_outputs(messages, keep_recent=0)
    assert elided == 0
    _, body = unwrap_channel(messages[2]["content"])
    assert "precious output" in body


def test_elide_keeps_recent_messages_untouched():
    recent = _tool_results_message(
        2,
        "web_search",
        "fresh output " + "f" * 200,
        "/tool_outputs/0002_web_search.txt",
    )
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        _tool_results_message(
            1,
            "web_search",
            "old output " + "o" * 200,
            "/tool_outputs/0001_web_search.txt",
        ),
        {"role": "assistant", "content": "working"},
        recent,
    ]
    elided = elide_tool_outputs(messages, keep_recent=2)
    assert elided == 1
    _, recent_body = unwrap_channel(recent["content"])
    assert "fresh output" in recent_body
    _, old_body = unwrap_channel(messages[2]["content"])
    assert "old output" not in old_body


def test_elide_is_idempotent():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        _tool_results_message(
            1, "web_search", "body " + "b" * 200, "/tool_outputs/0001_web_search.txt"
        ),
    ]
    assert elide_tool_outputs(messages, keep_recent=0) == 1
    assert elide_tool_outputs(messages, keep_recent=0) == 0
    _, body = unwrap_channel(messages[2]["content"])
    assert body.count("output elided") == 1


def test_elide_min_savings_keeps_marginal_rewrites_inline():
    """A rewrite that saves fewer than the threshold tokens is a net loss.

    The excerpt is nearly as long as the output, while the cache suffix
    after the rewritten message re-bills whole - so marginal rewrites
    must stay inline with their pointer attached.
    """
    marginal = _tool_results_message(
        1, "web_search", "m" * 700, "/tool_outputs/0001_web_search.txt"
    )
    worth_it = _tool_results_message(
        2, "web_search", "w" * 2500, "/tool_outputs/0002_web_search.txt"
    )
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        marginal,
        worth_it,
    ]
    elided = elide_tool_outputs(
        messages, keep_recent=0, keep_chars=200, min_savings_tokens=250
    )
    assert elided == 1
    _, marginal_body = unwrap_channel(marginal["content"])
    assert "mmmm" in marginal_body, "a marginal rewrite must stay inline"
    assert "saved_to=" in marginal_body, "pointer stays attached"
    assert "output elided" not in marginal_body
    _, worth_body = unwrap_channel(worth_it["content"])
    assert "output elided" in worth_body, "a real saving is taken"


def test_elide_suffix_rule_demands_more_when_history_is_long():
    """The rewrite threshold scales with the tokens after the message.

    Rewriting re-bills the suffix at the uncached rate once per elision
    cadence window, so a small saving cannot repay a large suffix: the
    threshold rises to suffix / horizon.
    """
    old_output = _tool_results_message(
        1, "web_search", "o" * 2000, "/tool_outputs/0001_web_search.txt"
    )
    filler = [{"role": "assistant", "content": "f" * 4000} for _ in range(6)]
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        old_output,
        *filler,
    ]
    elided = elide_tool_outputs(
        messages, keep_recent=len(filler), keep_chars=200, min_savings_tokens=0
    )
    assert elided == 0, (
        "a ~380-token saving cannot repay re-billing a ~6000-token suffix"
    )

    big_output = _tool_results_message(
        2, "web_search", "o" * 20000, "/tool_outputs/0002_web_search.txt"
    )
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        big_output,
        *filler,
    ]
    elided = elide_tool_outputs(
        messages, keep_recent=len(filler), keep_chars=200, min_savings_tokens=0
    )
    assert elided == 1, "a ~4900-token saving repays the re-bill"


def test_elide_horizon_zero_disables_the_suffix_rule():
    old_output = _tool_results_message(
        1, "web_search", "o" * 2000, "/tool_outputs/0001_web_search.txt"
    )
    filler = [{"role": "assistant", "content": "f" * 4000} for _ in range(6)]
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        old_output,
        *filler,
    ]
    elided = elide_tool_outputs(
        messages,
        keep_recent=len(filler),
        keep_chars=200,
        min_savings_tokens=0,
        request_horizon=0,
    )
    assert elided == 1, (
        "a zero horizon is the budget-exceeded path: fit the budget, ignore cache"
    )


def test_elide_keeps_head_and_tail_excerpt():
    output = "HEAD-LINE\n" + "m" * 3000 + "\nTAIL-LINE"
    message = _tool_results_message(
        1, "web_search", output, "/tool_outputs/0001_web_search.txt"
    )
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        message,
    ]
    elided = elide_tool_outputs(
        messages, keep_recent=0, keep_chars=200, min_savings_tokens=250
    )
    assert elided == 1
    _, body = unwrap_channel(message["content"])
    assert "HEAD-LINE" in body, "head stays so the model recalls the shape"
    assert "TAIL-LINE" in body, "tail stays: command conclusions sit there"
    assert "first and last 200 chars kept" in body
    assert "full text in /tool_outputs/0001_web_search.txt" in body
    assert body.count("m") < 600, (
        "only the head and tail excerpts keep m's; the 3000-char middle is gone"
    )
    assert "[...]" in body


def test_elide_excerpt_never_grows_a_result():
    output = "short but over min_chars padding" + "y" * 320
    message = _tool_results_message(
        1, "web_search", output, "/tool_outputs/0001_web_search.txt"
    )
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        message,
    ]
    elided = elide_tool_outputs(messages, keep_recent=0, keep_chars=200)
    assert elided == 0, "excerpt would be no shorter; nothing to remove"
    _, body = unwrap_channel(message["content"])
    assert "output elided" not in body
    assert "short but over min_chars padding" in body


def test_elide_keep_chars_zero_gives_pointer_only():
    message = _tool_results_message(
        1, "web_search", "z" * 1000, "/tool_outputs/0001_web_search.txt"
    )
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        message,
    ]
    elided = elide_tool_outputs(messages, keep_recent=0, keep_chars=0)
    assert elided == 1
    _, body = unwrap_channel(message["content"])
    assert "[output elided; full text in /tool_outputs/0001_web_search.txt]" in body
    assert "zzzz" not in body


def test_elide_images_replaces_old_attachments_with_pointers():
    old_image = _image_message("/tool_outputs/img1.png", 1200)
    recent_image = _image_message("/tool_outputs/img2.png", 1300)
    messages = [
        {"role": "system", "content": "system prompt"},
        old_image,
        recent_image,
    ]
    before = estimate_history_tokens(messages)

    assert elide_images(messages, keep_recent=1) == 1

    assert isinstance(old_image["content"], str), (
        "the attachment is replaced by a text pointer"
    )
    assert "image elided" in old_image["content"]
    assert "/tool_outputs/img1.png" in old_image["content"]
    assert old_image["image_tokens"] == 0, "the budget must stop counting it"
    assert isinstance(recent_image["content"], list), "recent window untouched"
    after = estimate_history_tokens(messages)
    assert before - after > 1000, "eliding must free the image's tokens"
    assert elide_images(messages, keep_recent=1) == 0, "idempotent"


def test_elide_images_respect_the_savings_floor():
    """The flat floor gates images: a tiny attachment is not worth rewriting."""
    worthwhile = _image_message("/tool_outputs/img1.png", 1200)
    messages = [
        {"role": "system", "content": "system prompt"},
        worthwhile,
    ]
    assert elide_images(messages, keep_recent=0, min_savings_tokens=250) == 1
    assert worthwhile["image_tokens"] == 0

    tiny = _image_message("/tool_outputs/img2.png", 100)
    messages = [
        {"role": "system", "content": "system prompt"},
        tiny,
    ]
    assert elide_images(messages, keep_recent=0, min_savings_tokens=250) == 0, (
        "a ~70-token saving is below the floor"
    )
    assert isinstance(tiny["content"], list), "the attachment stays untouched"


def test_elide_images_never_touches_plain_messages():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "just text"},
        {"role": "assistant", "content": [{"type": "text", "text": "reply"}]},
    ]
    assert elide_images(messages, keep_recent=0) == 0
    assert messages[1]["content"] == "just text"
    assert messages[2]["content"] == [{"type": "text", "text": "reply"}]


def test_recent_window_size_caps_by_tokens():
    big = {"role": "user", "content": "x" * 4000}
    small = {"role": "user", "content": "y" * 40}
    messages = [big, small, small]

    assert recent_window_size(messages, 3, 0) == 3, "zero disables the cap"
    assert recent_window_size(messages, 2, 50000) == 2, "cap above window"
    assert recent_window_size(messages, 3, 1000) == 2, "window shrinks to fit"
    assert recent_window_size(messages, 1, 50000) == 1, "count bound holds"
    assert recent_window_size(messages, 3, 10) == 1, "the newest message stays"
    assert recent_window_size(messages, 0, 50000) == 0, "count zero stays zero"


def test_elide_keep_recent_larger_than_history_protects_everything():
    """A window covering the whole list must not wrap around and elide.

    `last_editable` goes negative when the recent window exceeds the
    message count, and the negative slice stop wraps the range back
    into the middle of the list - eliding messages the window promised
    to protect.
    """
    tool_one = _tool_results_message(
        1, "web_search", "1" * 2000, "/tool_outputs/0001_web_search.txt"
    )
    tool_two = _tool_results_message(
        2, "web_search", "2" * 2000, "/tool_outputs/0002_web_search.txt"
    )
    for keep_recent in (3, 4, 5, 6, 12):
        messages = [
            {"role": "system", "content": "system prompt"},
            tool_one,
            tool_two,
        ]
        elided = elide_tool_outputs(
            messages, keep_recent=keep_recent, keep_chars=0, min_savings_tokens=0
        )
        assert elided == 0, f"keep_recent={keep_recent} must protect everything"
        assert "output elided" not in str(messages)


def test_recent_window_size_counts_image_messages_by_their_estimate():
    """An image inside the cap consumes its `image_tokens`, not its base64."""
    image = _image_message("/tool_outputs/img1.png", 1200)
    small = {"role": "user", "content": "y" * 40}
    messages = [image, small]

    assert recent_window_size(messages, 2, 1300) == 2, (
        "both fit: the image costs ~1200 tokens, the text ~14"
    )
    assert recent_window_size(messages, 2, 1210) == 1, (
        "the cap admits the image alone and the newest message always stays"
    )


def test_recent_window_size_counts_the_system_message_on_short_histories():
    """The documented inclusion: the system prompt eats into the cap."""
    system = {"role": "system", "content": "s" * 4000}
    small = {"role": "user", "content": "y" * 40}
    messages = [system, small, small]

    assert recent_window_size(messages, 3, 4200) == 3, (
        "the window fits: ~1004 tokens of system plus two small messages"
    )
    assert recent_window_size(messages, 3, 1031) == 2, (
        "the cap counts the system message too, shrinking the window"
    )


def test_periodic_cache_summary_preserves_totals():
    from syna.conversation.session import (
        CACHE_SUMMARY_INTERVAL_REQUESTS,
        AgentSession,
    )

    session = AgentSession(session_id="cache-summary-test")
    for _ in range(CACHE_SUMMARY_INTERVAL_REQUESTS):
        session._record_usage(100, 50, 0.01)
    totals = session._usage_totals
    assert totals["requests"] == CACHE_SUMMARY_INTERVAL_REQUESTS, (
        "the periodic log must not reset the accumulating totals"
    )
    assert totals["prompt_tokens"] == 100 * CACHE_SUMMARY_INTERVAL_REQUESTS
    assert abs(totals["cost"] - 0.2) < 1e-9


def test_estimate_correction_tracks_observed_ratio():
    from syna.conversation.session import (
        ESTIMATE_CORRECTION_MAX,
        ESTIMATE_CORRECTION_MIN,
        AgentSession,
    )

    session = AgentSession(session_id="correction-test")
    session._pending_request_estimate = 1000

    session._update_estimate_correction(1250)
    assert session._estimate_correction == 1.25
    assert session._pending_request_estimate is None, "pairing is consumed"

    session._update_estimate_correction(None)
    assert session._estimate_correction == 1.25, "no actual keeps the value"

    session._pending_request_estimate = 1000
    session._update_estimate_correction(9000)
    assert session._estimate_correction == ESTIMATE_CORRECTION_MAX, "clamped high"

    session._pending_request_estimate = 1000
    session._update_estimate_correction(100)
    assert session._estimate_correction == ESTIMATE_CORRECTION_MIN, "clamped low"


def test_budget_decision_uses_the_observed_correction(monkeypatch):
    from syna.conversation.session import AgentSession

    session = AgentSession(session_id="correction-budget-test")
    session.dump_path = Path(tempfile.mkdtemp(prefix="syna-correction-")) / "dump.json"
    session.context_budget_tokens = 4500
    session.max_history_messages = 10**6
    session.messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "x" * 16000},
    ]
    summarized = []
    monkeypatch.setattr(
        session, "_summarize_history", lambda history, logger: summarized.append(1)
    )

    session._estimate_correction = 1.0
    session._compact_history_if_needed()
    assert not summarized, "raw estimate under budget must not compact"

    session._estimate_correction = 1.5
    session._compact_history_if_needed()
    assert summarized, "the corrected total over budget must compact"


def test_pod_host_config_assembles_limits():
    host_config = build_pod_host_config(memory_mb=1024, pids_limit=64)
    assert host_config == {
        "Init": True,
        "Memory": 1024 * 1024 * 1024,
        "PidsLimit": 64,
    }


def test_pod_host_config_allows_unset_limits():
    host_config = build_pod_host_config(memory_mb=0, pids_limit=0)
    assert host_config == {"Init": True}


def test_create_container_self_heals_stale_endpoint():
    from _runner import MonkeyPatch

    from syna import state

    class FlakyClient:
        """Fails its first container run on a dead pipe, then works."""

        def __init__(self):
            self.runs = 0

        def run_container(self, image, **kwargs):
            self.runs += 1
            if self.runs == 1:
                raise FileNotFoundError(
                    2, "No such file or directory", "\\\\.\\pipe\\docker_engine"
                )
            return f"container-{self.runs}"

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    patch = MonkeyPatch()
    flaky = FlakyClient()
    fake_stack = type(
        "FakeStack",
        (),
        {"closed": False, "close": lambda self: setattr(self, "closed", True)},
    )()
    patch.setattr(state, "client", flaky)
    patch.setattr(state, "_exit_stack", fake_stack)
    patch.setattr(state, "_global_container", None)
    patch.setattr(state, "build_pod_host_config", lambda **k: {"Init": True})
    patch.setattr(state, "init_docker_client", lambda: setattr(state, "client", flaky))
    try:
        container = state.create_container()
        assert container == "container-2", "the retry after the reset must succeed"
        assert state.client is flaky
    finally:
        patch.undo()


def test_create_container_non_connection_error_propagates():
    from _runner import MonkeyPatch

    from syna import state

    class HostileClient:
        def run_container(self, image, **kwargs):
            raise RuntimeError(
                "Docker API Error (500): OCI runtime attempted to invoke a"
                " command that was not found"
            )

    patch = MonkeyPatch()
    patch.setattr(state, "client", HostileClient())
    init_calls = []
    patch.setattr(state, "init_docker_client", lambda: init_calls.append(1))
    try:
        try:
            state.create_container()
        except RuntimeError as exc:
            assert "500" in str(exc)
        else:
            raise AssertionError("a non-connection error must propagate")
        assert not init_calls, "no client reset for a non-connection error"
    finally:
        patch.undo()


def test_connection_error_classification():
    from syna import state

    assert state._is_connection_error(
        FileNotFoundError(2, "No such file or directory", "\\\\.\\pipe\\docker_engine")
    )
    assert state._is_connection_error(ConnectionError("refused"))
    assert state._is_connection_error(
        RuntimeError("No Docker or Podman API endpoint is reachable.")
    )
    assert not state._is_connection_error(RuntimeError("Docker API Error (500): boom"))
    assert not state._is_connection_error(ValueError("bad arguments"))


def _make_tool_call_reply(name, args, call_id="call_1"):
    return FakeMessage(
        content=None,
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args),
                },
            }
        ],
    )


def _make_text_reply(text):
    return FakeMessage(content=text, tool_calls=None)


def test_session_elides_old_outputs_under_token_budget():
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    pod = FakePod()
    register_container(pod)
    try:
        from syna.conversation.session import AgentSession

        session = AgentSession(session_id="context-test")
        session.dump_path = Path(tempfile.mkdtemp(prefix="syna-context-")) / "dump.json"
        session.request_delay = 0
        session.context_budget_tokens = 14000
        session.elide_keep_recent_messages = 0
        session.client = FakeChat(
            [
                _make_tool_call_reply("smoke_tool", {"label": "x" * 24000}),
                _make_text_reply("done"),
            ]
        )
        session.run_turn("probe")
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    assert len(session.client.requests) == 2, "summarization must not run"
    assert session.client.replies == []

    tool_messages = [
        m
        for m in session.messages
        if is_channel_message(m.get("content") or "", "TOOL_RESULTS")
    ]
    assert len(tool_messages) == 1
    _, body = unwrap_channel(tool_messages[0]["content"])
    assert "output elided" in body
    assert "first and last 200 chars kept" in body, "excerpt survives the budget path"
    assert "full text in /tool_outputs/" in body
    assert body.count("x") < 400, "the middle of the output must be gone"
    assert body.rstrip().endswith("</result>")


def test_session_elides_old_outputs_on_message_interval():
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    pod = FakePod()
    register_container(pod)
    try:
        from syna.conversation.session import AgentSession

        session = AgentSession(session_id="context-interval-test")
        session.dump_path = Path(tempfile.mkdtemp(prefix="syna-context-")) / "dump.json"
        session.request_delay = 0
        session.context_budget_tokens = 10**9
        session.max_history_messages = 10**6
        session.elide_keep_recent_messages = 1
        session.elide_interval_messages = 2
        session.elide_every_requests = 1
        session.elide_min_savings_tokens = 0
        session.elide_keep_chars = 0
        session.client = FakeChat(
            [
                _make_tool_call_reply(
                    "smoke_tool", {"label": "first " + "a" * 200}, call_id="call_1"
                ),
                _make_tool_call_reply(
                    "smoke_tool", {"label": "second"}, call_id="call_2"
                ),
                _make_text_reply("done"),
            ]
        )
        session.run_turn("probe")
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    assert not session.client.replies, "summarization must not run"

    tool_messages = [
        m
        for m in session.messages
        if is_channel_message(m.get("content") or "", "TOOL_RESULTS")
    ]
    assert len(tool_messages) == 2
    _, first_body = unwrap_channel(tool_messages[0]["content"])
    _, second_body = unwrap_channel(tool_messages[1]["content"])
    assert "[output elided; full text in /tool_outputs/" in first_body
    assert "aaaa" not in first_body
    assert "ran: second" in second_body, "recent output must stay intact"


def test_session_keeps_short_outputs_full_text_on_interval_path():
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    pod = FakePod()
    register_container(pod)
    try:
        from syna.conversation.session import AgentSession

        session = AgentSession(session_id="context-short-test")
        session.dump_path = Path(tempfile.mkdtemp(prefix="syna-short-")) / "dump.json"
        session.request_delay = 0
        session.context_budget_tokens = 10**9
        session.max_history_messages = 10**6
        session.elide_keep_recent_messages = 1
        session.elide_interval_messages = 2
        session.elide_every_requests = 1
        session.client = FakeChat(
            [
                _make_tool_call_reply(
                    "smoke_tool", {"label": "short"}, call_id="call_1"
                ),
                _make_tool_call_reply(
                    "smoke_tool", {"label": "x" * 9}, call_id="call_2"
                ),
                _make_text_reply("done"),
            ]
        )
        session.run_turn("probe")
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    assert not session.client.replies, "summarization must not run"
    tool_messages = [
        m
        for m in session.messages
        if is_channel_message(m.get("content") or "", "TOOL_RESULTS")
    ]
    assert len(tool_messages) == 2
    first = unwrap_channel(tool_messages[0]["content"])[1]
    assert "ran: short" in first, "short output must stay inline on the interval path"
    assert "saved_to=" in first, "pointer stays attached"
    assert "output elided" not in first
    assert "output elided" not in str(session.messages), (
        "no output is long enough to elide"
    )


def test_session_elision_is_batched_by_request_interval():
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    pod = FakePod()
    register_container(pod)
    try:
        from syna.conversation.session import AgentSession

        session = AgentSession(session_id="context-batch-test")
        session.dump_path = Path(tempfile.mkdtemp(prefix="syna-batch-")) / "dump.json"
        session.request_delay = 0
        session.context_budget_tokens = 10**9
        session.max_history_messages = 10**6
        session.elide_keep_recent_messages = 1
        session.elide_interval_messages = 2
        session.elide_every_requests = 10
        session.client = FakeChat(
            [
                _make_tool_call_reply(
                    "smoke_tool", {"label": "first"}, call_id="call_1"
                ),
                _make_tool_call_reply(
                    "smoke_tool", {"label": "second"}, call_id="call_2"
                ),
                _make_tool_call_reply(
                    "smoke_tool", {"label": "third"}, call_id="call_3"
                ),
                _make_text_reply("done"),
            ]
        )
        session.run_turn("probe")
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    third_prompt = session.client.requests[2]["messages"]
    assert any("ran: first" in str(m.get("content")) for m in third_prompt), (
        "outputs must stay full-text between batched elision passes"
    )
    assert not any(
        "output elided" in str(m.get("content")) for m in session.messages
    ), "no batched elision pass is due within the request budget"


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.refusal = None


class FakeChoice:
    def __init__(self, message):
        self.message = message
        self.finish_reason = (
            "stop" if not getattr(message, "tool_calls", None) else "tool_calls"
        )


class FakeResponse:
    def __init__(self, message):
        self.choices = [FakeChoice(message)]


class FakeChat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = self

    def _snapshot(self, request):
        """Return what send() records; subclasses may freeze the request."""
        return request

    def send(self, **request):
        self.requests.append(self._snapshot(request))
        reply = self.replies.pop(0)
        if isinstance(reply, FakeMessage):
            return FakeResponse(reply)
        return FakeResponse(FakeMessage(content=reply))


class SnapshotChat(FakeChat):
    """FakeChat that records a frozen copy of every request.

    The real SDK serializes the request at send time, while a plain
    FakeChat keeps a live reference to the evolving history list, so a
    meaningful prefix-stability check needs the frozen copy.
    """

    def _snapshot(self, request):
        import copy

        return copy.deepcopy(request)


def test_request_prefix_is_stable_across_steps():
    """Consecutive requests must share a byte-identical prompt prefix.

    Provider-side prompt caches match the request prefix exactly, so a
    mutation of any earlier message between steps invalidates the cache
    from that message onward and re-bills the whole history. Elision,
    compaction, and purge-after-repair are the documented exceptions;
    this test pins the append-only invariant for the common path.
    """
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    pod = FakePod()
    register_container(pod)
    try:
        from syna.conversation.session import AgentSession

        session = AgentSession(session_id="prefix-stability-test")
        session.dump_path = Path(tempfile.mkdtemp(prefix="syna-prefix-")) / "dump.json"
        session.request_delay = 0
        session.context_budget_tokens = 10**9
        session.max_history_messages = 10**6
        session.elide_keep_recent_messages = 0
        session.elide_interval_messages = 10**6
        session.elide_every_requests = 10**6
        session.client = SnapshotChat(
            [
                _make_tool_call_reply(
                    "smoke_tool", {"label": "first"}, call_id="call_1"
                ),
                _make_tool_call_reply(
                    "smoke_tool", {"label": "second"}, call_id="call_2"
                ),
                _make_text_reply("done"),
            ]
        )
        session.run_turn("probe")
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    requests = session.client.requests
    assert len(requests) == 3, "three model requests expected"
    for previous, current in itertools.pairwise(requests):
        history = previous["messages"]
        current_prefix = current["messages"][: len(history)]
        assert json.dumps(current_prefix) == json.dumps(history), (
            "an earlier message changed between steps, which invalidates"
            " the provider prompt cache from that message onward"
        )
        assert len(current["messages"]) > len(history), (
            "each step must append new history"
        )
        assert json.dumps(current["tools"]) == json.dumps(previous["tools"]), (
            "the tool definitions must stay identical across steps"
        )
    assert requests[0]["messages"][0]["role"] == "system"


def main():
    run_suite(
        [
            test_estimate_tokens_bounds,
            test_estimate_tokens_is_script_aware,
            test_estimate_tokens_tracks_real_tokenizer_counts,
            test_estimate_history_tokens_counts_every_message,
            test_elide_replaces_old_saved_outputs_with_pointers,
            test_elide_keeps_results_without_saved_to,
            test_elide_keeps_recent_messages_untouched,
            test_elide_is_idempotent,
            test_elide_min_savings_keeps_marginal_rewrites_inline,
            test_elide_suffix_rule_demands_more_when_history_is_long,
            test_elide_horizon_zero_disables_the_suffix_rule,
            test_elide_keeps_head_and_tail_excerpt,
            test_elide_excerpt_never_grows_a_result,
            test_elide_keep_chars_zero_gives_pointer_only,
            test_elide_images_replaces_old_attachments_with_pointers,
            test_elide_images_respect_the_savings_floor,
            test_elide_images_never_touches_plain_messages,
            test_recent_window_size_caps_by_tokens,
            test_recent_window_size_counts_image_messages_by_their_estimate,
            test_recent_window_size_counts_the_system_message_on_short_histories,
            test_elide_keep_recent_larger_than_history_protects_everything,
            test_pod_host_config_assembles_limits,
            test_pod_host_config_allows_unset_limits,
            test_create_container_self_heals_stale_endpoint,
            test_create_container_non_connection_error_propagates,
            test_connection_error_classification,
            test_session_elides_old_outputs_under_token_budget,
            test_session_elides_old_outputs_on_message_interval,
            test_session_elision_is_batched_by_request_interval,
            test_request_prefix_is_stable_across_steps,
            test_periodic_cache_summary_preserves_totals,
            test_estimate_correction_tracks_observed_ratio,
            test_budget_decision_uses_the_observed_correction,
        ]
    )
    print("ALL CONTEXT AND POD LIMIT TESTS PASSED")


if __name__ == "__main__":
    main()
