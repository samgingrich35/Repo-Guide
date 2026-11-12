"""Chat with Repo Guide from the terminal. Handy for testing without the web page.

    python -m app.cli https://github.com/pallets/flask

Type a question and press Enter; an empty line (or Ctrl+C) quits.
"""

import sys

import anthropic

from app.agent import AgentResult, RepoAgent
from app.config import get_settings
from app.sessions import OVERVIEW_PROMPT
from app.tools import RepoNotFoundError, RepoTools, parse_repo_url


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    settings = get_settings()
    if not settings.anthropic_api_key:
        print("No ANTHROPIC_API_KEY found. Copy .env.example to .env and add your key.")
        return 1

    try:
        owner, repo = parse_repo_url(argv[1])
        tools = RepoTools(owner, repo, token=settings.github_token)
        tools.get_repo_info()
    except RepoNotFoundError as e:
        print(e)
        return 1

    agent = RepoAgent(
        client=anthropic.Anthropic(api_key=settings.anthropic_api_key),
        tools=tools,
        model=settings.claude_model,
        max_steps=settings.max_agent_steps,
        effort=settings.effort,
    )
    print(f"Exploring {owner}/{repo} with {settings.claude_model}...\n")
    show(agent.ask(OVERVIEW_PROMPT))

    try:
        while question := input("\nYou> ").strip():
            show(agent.ask(question))
    except (KeyboardInterrupt, EOFError):
        pass
    return 0


def show(result: AgentResult) -> None:
    for step in result.steps:
        print(f"  [tool] {step.tool} {step.input}{'  (error)' if step.is_error else ''}")
    print("\n" + result.answer)
    cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "unknown"
    print(f"\n  ({len(result.steps)} steps, ~{cost}, stop={result.stop})")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
