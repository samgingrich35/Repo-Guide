"""The agent's "hands": tools that fetch information from a GitHub repo.

Claude never touches GitHub directly. It asks to use one of these tools by
name, our code runs the tool, and the text result goes back to Claude.

Every result is plain text and size-capped, so a giant repo or file can't
blow up the conversation (or the bill).
"""

import re
from dataclasses import dataclass, field

import httpx

GITHUB_API = "https://api.github.com"

MAX_FILE_BYTES = 200_000  # refuse to read files bigger than this
MAX_LINES_PER_READ = 400  # Claude can ask for more with start_line
MAX_LIST_ENTRIES = 200
MAX_SEARCH_RESULTS = 20

_REPO_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?(?:[/?#].*)?$"
)


class ToolError(Exception):
    """A problem worth telling Claude about (bad path, file too big, ...)."""


class RepoNotFoundError(ToolError):
    """The repo doesn't exist, is private, or the URL is malformed."""


def parse_repo_url(url: str) -> tuple[str, str]:
    """Turn 'https://github.com/pallets/flask/tree/main' into ('pallets', 'flask')."""
    match = _REPO_URL.match(url.strip())
    if not match:
        raise RepoNotFoundError(f"Not a GitHub repo URL: {url!r}")
    return match["owner"], match["repo"]


# What Claude sees: a name, a description of when to use it, and the inputs.
TOOL_DEFINITIONS = [
    {
        "name": "get_repo_info",
        "description": (
            "Get the repository's description, main language, stars, topics, license and "
            "default branch. Good first step for understanding what a project is."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_files",
        "description": (
            "List the files and folders directly inside a folder of the repo. Folders show how "
            "many files they contain. Use an empty path for the repo root."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Folder path, e.g. 'src/app'. Empty string for the root.",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_code",
        "description": (
            "Find files related to a word or phrase. Matches file paths, and also file contents "
            "when available. Use it to locate where something is defined or used."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to search for."}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            f"Read a text file, returned with line numbers. Long files are returned "
            f"{MAX_LINES_PER_READ} lines at a time; pass start_line to continue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path, e.g. 'src/app/main.py'."},
                "start_line": {"type": "integer", "description": "First line to read (1-based)."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class RepoTools:
    """Tools bound to one GitHub repo. Create one per chat session."""

    owner: str
    repo: str
    token: str = ""
    http: httpx.Client | None = None
    files_read: list[str] = field(default_factory=list)  # for citing sources
    _info: dict | None = field(default=None, repr=False)
    _tree: list[dict] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.http is None:
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "repo-guide"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self.http = httpx.Client(base_url=GITHUB_API, headers=headers, timeout=20)

    # ---------- dispatcher ----------

    def run(self, name: str, tool_input: dict) -> tuple[str, bool]:
        """Run a tool by name. Returns (text_result, is_error). Never raises."""
        handlers = {
            "get_repo_info": lambda: self.get_repo_info(),
            "list_files": lambda: self.list_files(tool_input.get("path", "")),
            "search_code": lambda: self.search_code(tool_input.get("query", "")),
            "read_file": lambda: self.read_file(
                tool_input.get("path", ""), tool_input.get("start_line", 1)
            ),
        }
        if name not in handlers:
            return f"Unknown tool: {name}", True
        try:
            return handlers[name](), False
        except ToolError as e:
            return f"Error: {e}", True
        except httpx.HTTPError as e:
            return f"Error talking to GitHub: {e}", True

    # ---------- the four tools ----------

    def get_repo_info(self) -> str:
        info = self._repo_info()
        lines = [
            f"Repository: {info['full_name']}",
            f"Description: {info.get('description') or '(none)'}",
            f"Main language: {info.get('language') or 'unknown'}",
            f"Stars: {info.get('stargazers_count', 0)}",
            f"Topics: {', '.join(info.get('topics') or []) or '(none)'}",
            f"License: {(info.get('license') or {}).get('name') or '(none)'}",
            f"Default branch: {info['default_branch']}",
        ]
        return "\n".join(lines)

    def list_files(self, path: str = "") -> str:
        prefix = _clean_path(path)
        prefix_slash = f"{prefix}/" if prefix else ""
        tree = self._file_tree()

        if prefix and not any(e["path"] == prefix and e["type"] == "tree" for e in tree):
            raise ToolError(f"No folder named '{prefix}'. Use list_files('') to see the root.")

        folders: dict[str, int] = {}
        files: list[str] = []
        for entry in tree:
            if not entry["path"].startswith(prefix_slash):
                continue
            rest = entry["path"][len(prefix_slash) :]
            if "/" in rest:  # something deeper: count it toward its top folder
                top = rest.split("/", 1)[0]
                if entry["type"] == "blob":
                    folders[top] = folders.get(top, 0) + 1
            elif entry["type"] == "tree":
                folders.setdefault(rest, 0)
            else:
                files.append(f"{rest}  ({_human_size(entry.get('size', 0))})")

        lines = [f"{name}/  ({count} files)" for name, count in sorted(folders.items())]
        lines += sorted(files)
        if not lines:
            return f"'{prefix or '/'}' is empty."
        shown = lines[:MAX_LIST_ENTRIES]
        if len(lines) > MAX_LIST_ENTRIES:
            shown.append(f"... and {len(lines) - MAX_LIST_ENTRIES} more")
        return f"Contents of '{prefix or '/'}':\n" + "\n".join(shown)

    def search_code(self, query: str) -> str:
        query = query.strip()
        if not query:
            raise ToolError("Search query is empty.")

        content_hits = self._search_contents(query) if self.token else []
        q = query.lower()
        path_hits = [
            e["path"] for e in self._file_tree() if e["type"] == "blob" and q in e["path"].lower()
        ]

        results = list(dict.fromkeys(content_hits + path_hits))[:MAX_SEARCH_RESULTS]
        if not results:
            note = "" if self.token else " (only file paths were searched; no GitHub token set)"
            return f"No matches for '{query}'{note}."
        header = f"Files matching '{query}'"
        if not self.token:
            header += " (file paths only)"
        return header + ":\n" + "\n".join(results)

    def read_file(self, path: str, start_line: int = 1) -> str:
        path = _clean_path(path)
        entry = next((e for e in self._file_tree() if e["path"] == path), None)
        if entry is None or entry["type"] != "blob":
            raise ToolError(f"No file named '{path}'. Use list_files or search_code to find it.")
        if entry.get("size", 0) > MAX_FILE_BYTES:
            raise ToolError(f"'{path}' is {_human_size(entry['size'])}, too large to read.")

        resp = self.http.get(
            f"/repos/{self.owner}/{self.repo}/contents/{path}",
            params={"ref": self._branch()},
            headers={"Accept": "application/vnd.github.raw"},
        )
        _raise_for_github(resp)
        if b"\x00" in resp.content[:8000]:
            raise ToolError(f"'{path}' looks like a binary file (image, archive, ...).")

        all_lines = resp.content.decode("utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        end = min(len(all_lines), start + MAX_LINES_PER_READ - 1)
        if path not in self.files_read:
            self.files_read.append(path)

        numbered = [f"{n:>5}  {all_lines[n - 1]}" for n in range(start, end + 1)]
        header = f"{path} (lines {start}-{end} of {len(all_lines)})"
        if end < len(all_lines):
            header += f". More remains: call read_file with start_line={end + 1}"
        return header + "\n" + "\n".join(numbered)

    # ---------- helpers (cached so we don't re-download per tool call) ----------

    def _repo_info(self) -> dict:
        if self._info is None:
            resp = self.http.get(f"/repos/{self.owner}/{self.repo}")
            if resp.status_code == 404:
                raise RepoNotFoundError(
                    f"Repo {self.owner}/{self.repo} not found (it may be private)."
                )
            _raise_for_github(resp)
            self._info = resp.json()
        return self._info

    def _branch(self) -> str:
        return self._repo_info()["default_branch"]

    def _file_tree(self) -> list[dict]:
        if self._tree is None:
            resp = self.http.get(
                f"/repos/{self.owner}/{self.repo}/git/trees/{self._branch()}",
                params={"recursive": "1"},
            )
            _raise_for_github(resp)
            self._tree = resp.json().get("tree", [])
        return self._tree

    def _search_contents(self, query: str) -> list[str]:
        """GitHub code search (needs a token). Falls back to [] on any problem."""
        resp = self.http.get(
            "/search/code",
            params={"q": f"{query} repo:{self.owner}/{self.repo}", "per_page": MAX_SEARCH_RESULTS},
        )
        if resp.status_code != 200:
            return []
        return [item["path"] for item in resp.json().get("items", [])]


def _clean_path(path: str) -> str:
    return (path or "").strip().strip("/").removeprefix("./")


def _human_size(size: int) -> str:
    return f"{size} B" if size < 1024 else f"{size / 1024:.1f} KB"


def _raise_for_github(resp: httpx.Response) -> None:
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        raise ToolError("GitHub rate limit reached. Try again later or set GITHUB_TOKEN.")
    if resp.status_code == 404:
        raise ToolError("Not found on GitHub.")
    if resp.status_code >= 400:
        raise ToolError(f"GitHub returned HTTP {resp.status_code}.")
