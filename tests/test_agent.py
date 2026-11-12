"""Tests for the agent loop, using a scripted fake Claude: offline, free, and repeatable."""

from types import SimpleNamespace as NS

import pytest

from app.agent import STEP_LIMIT_NOTE, RepoAgent, Usage

# ---------- fakes ----------


def text(t):
    return NS(type="text", text=t)


def thinking():
    return NS(type="thinking", thinking="", signature="sig")


def tool_use(name, tool_input, id_="t1"):
    return NS(type="tool_use", id=id_, name=name, input=tool_input)


def reply(*content, stop="end_turn", tokens_in=100, tokens_out=20, cache_read=0):
    usage = NS(
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=cache_read,
    )
    return NS(content=list(content), stop_reason=stop, usage=usage)


class FakeClaude:
    """Plays back scripted replies and records every request it receives."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.messages = self  # so code can call client.messages.create(...)

    def create(self, **kwargs):
        # Snapshot the conversation as it was at call time.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self.replies:
            raise AssertionError("Agent made more Claude calls than the test scripted")
        return self.replies.pop(0)


class FakeTools:
    """Stands in for RepoTools: returns canned text and remembers files 'read'."""

    def __init__(self, fail=()):
        self.files_read = []
        self.calls = []
        self.fail = set(fail)

    def run(self, name, tool_input):
        self.calls.append((name, tool_input))
        if name in self.fail:
            return f"Error: {name} failed", True
        if name == "read_file":
            self.files_read.append(tool_input["path"])
        return f"result of {name}", False


def make_agent(claude, tools=None, **kwargs):
    return RepoAgent(client=claude, tools=tools or FakeTools(), model="claude-haiku-5-5", **kwargs)


# ---------- the basic loop ----------


def test_answers_directly_when_no_tools_are_needed():
    claude = FakeClaude(reply(text("Hello!")))
    result = make_agent(claude).ask("hi")

    assert result.answer == "Hello!"
    assert result.steps == []
    assert result.stop == "done"
    assert len(claude.requests) == 1


def test_runs_a_tool_then_answers_with_the_file_it_read():
    claude = FakeClaude(
        reply(tool_use("read_file", {"path": "app.py"}, id_="abc"), stop="tool_use"),
        reply(text("app.py starts the server.")),
    )
    tools = FakeTools()
    result = make_agent(claude, tools).ask("what does app.py do?")

    assert result.answer == "app.py starts the server."
    assert [s.tool for s in result.steps] == ["read_file"]
    assert result.files_read == ["app.py"]
    assert tools.calls == [("read_file", {"path": "app.py"})]

    # The tool result went back to Claude, matched to the right request id.
    tool_reply = claude.requests[1]["messages"][-1]
    assert tool_reply["role"] == "user"
    assert tool_reply["content"][0]["tool_use_id"] == "abc"
    assert tool_reply["content"][0]["content"] == "result of read_file"


def test_parallel_tool_calls_are_answered_in_one_message():
    claude = FakeClaude(
        reply(
            tool_use("get_repo_info", {}, id_="a"),
            tool_use("list_files", {"path": ""}, id_="b"),
            stop="tool_use",
        ),
        reply(text("done")),
    )
    make_agent(claude).ask("overview please")

    results = claude.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


def test_tool_errors_are_passed_to_claude_not_raised():
    claude = FakeClaude(
        reply(tool_use("read_file", {"path": "x"}), stop="tool_use"),
        reply(text("That file doesn't exist.")),
    )
    result = make_agent(claude, FakeTools(fail={"read_file"})).ask("read x")

    assert result.steps[0].is_error is True
    assert claude.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_thinking_blocks_are_not_shown_in_the_answer():
    claude = FakeClaude(reply(thinking(), text("Answer.")))
    assert make_agent(claude).ask("q").answer == "Answer."


# ---------- the step limit ----------


def test_step_limit_switches_tools_off_and_forces_an_answer():
    claude = FakeClaude(
        reply(tool_use("list_files", {"path": ""}, id_="1"), stop="tool_use"),
        reply(tool_use("list_files", {"path": "src"}, id_="2"), stop="tool_use"),
        reply(text("Best answer with what I found.")),
    )
    result = make_agent(claude, max_steps=2).ask("explain everything")

    assert len(result.steps) == 2
    assert result.stop == "step_limit"
    assert result.answer == "Best answer with what I found."
    assert claude.requests[0]["tool_choice"] == {"type": "auto"}
    assert claude.requests[2]["tool_choice"] == {"type": "none"}
    last_user_turn = claude.requests[2]["messages"][-1]["content"]
    assert last_user_turn[-1] == {"type": "text", "text": STEP_LIMIT_NOTE}


def test_never_loops_forever_even_if_claude_ignores_the_limit():
    claude = FakeClaude(
        reply(tool_use("list_files", {"path": ""}, id_="1"), stop="tool_use"),
        reply(
            text("Let me look more."),
            tool_use("list_files", {"path": "x"}, id_="2"),
            stop="tool_use",
        ),
        reply(text("Follow-up answer.")),
    )
    agent = make_agent(claude, max_steps=1)
    result = agent.ask("q1")
    assert result.stop == "step_limit"

    # The ignored tool call got a reply, and turns still alternate user/assistant.
    assert agent.messages[-1]["content"][0]["tool_use_id"] == "2"
    agent.ask("q2")
    roles = [m["role"] for m in claude.requests[-1]["messages"]]
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False)), roles


# ---------- memory across questions ----------


def test_follow_up_questions_include_the_earlier_conversation():
    claude = FakeClaude(reply(text("It's a web framework.")), reply(text("Mostly Python.")))
    agent = make_agent(claude)
    agent.ask("What is this repo?")
    agent.ask("What language?")

    sent = claude.requests[1]["messages"]
    assert len(sent) == 3  # question 1, answer 1, question 2
    assert sent[0]["content"][0]["text"] == "What is this repo?"
    assert sent[2]["content"][0]["text"] == "What language?"


def test_files_read_are_reported_per_question():
    claude = FakeClaude(
        reply(tool_use("read_file", {"path": "a.py"}), stop="tool_use"),
        reply(text("one")),
        reply(tool_use("read_file", {"path": "b.py"}), stop="tool_use"),
        reply(text("two")),
    )
    agent = make_agent(claude)
    assert agent.ask("q1").files_read == ["a.py"]
    assert agent.ask("q2").files_read == ["b.py"]


# ---------- unusual endings ----------


def test_refusal_gives_a_polite_message():
    result = make_agent(FakeClaude(reply(stop="refusal"))).ask("q")
    assert result.stop == "refusal"
    assert "can't help" in result.answer


def test_max_tokens_marks_the_answer_as_cut_off():
    result = make_agent(FakeClaude(reply(text("Partial..."), stop="max_tokens"))).ask("q")
    assert result.stop == "max_tokens"
    assert result.answer.startswith("Partial...")
    assert "cut off" in result.answer


# ---------- request shape & cost ----------


def test_requests_use_configured_model_effort_and_caching():
    claude = FakeClaude(reply(text("ok")))
    make_agent(claude, effort="low").ask("q")
    req = claude.requests[0]

    assert req["model"] == "claude-haiku-5-5"
    assert req["output_config"] == {"effort": "low"}
    assert req["cache_control"] == {"type": "ephemeral"}
    assert {t["name"] for t in req["tools"]} >= {"read_file", "list_files"}


def test_usage_is_added_up_across_the_loop():
    claude = FakeClaude(
        reply(tool_use("get_repo_info", {}), stop="tool_use", tokens_in=1000, tokens_out=50),
        reply(text("done"), tokens_in=200, tokens_out=100, cache_read=1000),
    )
    result = make_agent(claude).ask("q")

    assert result.usage.input_tokens == 1200
    assert result.usage.output_tokens == 150
    assert result.usage.cache_read_tokens == 1000
    assert result.cost_usd == pytest.approx(
        (1200 * 0.10 + 1000 * 0.10 * 0.10 + 150 * 0.50) / 1_000_000
    )


def test_cost_is_unknown_for_unlisted_models():
    assert Usage(input_tokens=10).cost_usd("some-future-model") is None
