"""The agent's "brain": the loop that lets Claude explore a repo with tools.

How one question is answered:

    1. Send Claude the conversation + the list of tools it may use.
    2. Claude replies either with a final answer (done!) or with
       "please run tool X with input Y".
    3. We run the tool, send the result back, and go to step 1.

A step limit stops runaway loops, and every request's token usage is
added up so we can log what each answer cost.
"""

import logging
from dataclasses import dataclass, field

import anthropic

from app.tools import TOOL_DEFINITIONS, RepoTools

log = logging.getLogger("repo_guide.agent")

SYSTEM_PROMPT = """\
You are Repo Guide, an expert software engineer who explains GitHub repositories \
to developers who have never seen them before.

You can explore the repository only through your tools. Use them before answering: \
never guess about code you have not looked at. Start broad (repo info, the root \
folder, the README), then narrow down to the files that matter for the question. \
Read only what you need; the repository may be large.

When you answer:
- Be clear and concrete, and write for a developer new to this codebase.
- Refer to files by their path in backticks, e.g. `src/app/main.py`.
- Use short sections and bullet points where they help.
- If you could not find something, say so plainly rather than inventing it."""

STEP_LIMIT_NOTE = (
    "You have reached the exploration step limit. Do not call any more tools. "
    "Answer now using what you have found, and mention anything you could not check."
)

# Price per million tokens: (input, output). Source: Anthropic pricing, Nov 2026.
PRICES_PER_MTOK = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
CACHE_WRITE_MULTIPLIER = 1.25  # writing to the prompt cache costs a bit more...
CACHE_READ_MULTIPLIER = 0.10  # ...but re-reading it is 90% cheaper


@dataclass
class Step:
    """One tool call the agent made, shown on the page so users can watch it work."""

    tool: str
    input: dict
    is_error: bool


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, usage) -> None:
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

    def cost_usd(self, model: str) -> float | None:
        """Estimated dollar cost, or None if we don't know the model's price."""
        if model not in PRICES_PER_MTOK:
            return None
        price_in, price_out = PRICES_PER_MTOK[model]
        total = (
            self.input_tokens * price_in
            + self.cache_write_tokens * price_in * CACHE_WRITE_MULTIPLIER
            + self.cache_read_tokens * price_in * CACHE_READ_MULTIPLIER
            + self.output_tokens * price_out
        )
        return total / 1_000_000


@dataclass
class AgentResult:
    answer: str
    steps: list[Step]
    files_read: list[str]
    usage: Usage
    cost_usd: float | None
    stop: str  # "done" | "step_limit" | "max_tokens" | "refusal"


@dataclass
class RepoAgent:
    """An agent bound to one repo. It remembers the conversation across questions."""

    client: anthropic.Anthropic
    tools: RepoTools
    model: str
    max_steps: int = 10
    effort: str = "medium"
    messages: list[dict] = field(default_factory=list)

    def ask(self, question: str) -> AgentResult:
        """Answer one question, using as many tool calls as needed (up to max_steps)."""
        self._add_user_text(question)
        steps: list[Step] = []
        usage = Usage()
        files_before = len(self.tools.files_read)
        stop = "done"

        while True:
            out_of_steps = len(steps) >= self.max_steps
            response = self._call_claude(allow_tools=not out_of_steps)
            usage.add(response.usage)
            # Keep Claude's reply exactly as returned: it's part of the memory.
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "pause_turn":
                continue  # the API paused a long turn; sending it back resumes it
            if response.stop_reason == "tool_use" and out_of_steps:
                # Shouldn't happen (tools are switched off at the limit), but never
                # loop forever. Every tool request needs a reply, so answer them.
                self._decline_tool_calls(response.content)
                stop = "step_limit"
                break
            if response.stop_reason != "tool_use":
                stop = {"max_tokens": "max_tokens", "refusal": "refusal"}.get(
                    response.stop_reason, "step_limit" if out_of_steps else "done"
                )
                break

            # Claude asked for one or more tools: run them all, reply in ONE message.
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                text, is_error = self.tools.run(block.name, block.input)
                steps.append(Step(tool=block.name, input=dict(block.input), is_error=is_error))
                log.info("tool %s %s error=%s", block.name, block.input, is_error)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": text,
                        "is_error": is_error,
                    }
                )
            if len(steps) >= self.max_steps:
                results.append({"type": "text", "text": STEP_LIMIT_NOTE})
            self.messages.append({"role": "user", "content": results})

        answer = _final_text(response.content, stop)
        cost = usage.cost_usd(self.model)
        log.info(
            "answered model=%s steps=%d in=%d out=%d cache_read=%d cost=%s stop=%s",
            self.model,
            len(steps),
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            f"${cost:.4f}" if cost is not None else "unknown",
            stop,
        )
        return AgentResult(
            answer=answer,
            steps=steps,
            files_read=self.tools.files_read[files_before:],
            usage=usage,
            cost_usd=cost,
            stop=stop,
        )

    def _add_user_text(self, text: str) -> None:
        """Add the user's question. Turns must alternate user/assistant, so if the
        last turn was already ours (declined tool calls), extend it instead."""
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages[-1]["content"].append({"type": "text", "text": text})
        else:
            self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})

    def _decline_tool_calls(self, content) -> None:
        declined = [
            {
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": "Not run: step limit reached.",
                "is_error": True,
            }
            for b in content
            if b.type == "tool_use"
        ]
        self.messages.append({"role": "user", "content": declined})

    def _call_claude(self, allow_tools: bool):
        return self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            # Same tools every call (just "none" at the limit) so the prompt cache stays valid.
            tool_choice={"type": "auto" if allow_tools else "none"},
            output_config={"effort": self.effort},
            # Cache the growing conversation: each loop step re-sends it, and cached
            # tokens cost 90% less.
            cache_control={"type": "ephemeral"},
            messages=self.messages,
        )


def _final_text(content, stop: str) -> str:
    text = "\n\n".join(b.text for b in content if b.type == "text").strip()
    if stop == "refusal":
        return text or "Sorry, I can't help with that request."
    if stop == "max_tokens":
        return (text + "\n\n_(Answer cut off because it hit the length limit.)_").strip()
    return text or "(No answer was produced.)"
