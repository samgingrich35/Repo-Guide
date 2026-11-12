"""Tests for the GitHub tools. GitHub is faked with httpx.MockTransport (offline, free)."""

import httpx
import pytest

from app.tools import GITHUB_API, RepoNotFoundError, RepoTools, parse_repo_url

REPO_INFO = {
    "full_name": "octo/demo",
    "description": "A demo repo",
    "language": "Python",
    "stargazers_count": 42,
    "topics": ["agents", "demo"],
    "license": {"name": "MIT License"},
    "default_branch": "main",
}

TREE = {
    "tree": [
        {"path": "README.md", "type": "blob", "size": 120},
        {"path": "app", "type": "tree"},
        {"path": "app/main.py", "type": "blob", "size": 300},
        {"path": "app/utils", "type": "tree"},
        {"path": "app/utils/helpers.py", "type": "blob", "size": 50},
        {"path": "app/utils/strings.py", "type": "blob", "size": 80},
        {"path": "assets/logo.png", "type": "blob", "size": 9000},
        {"path": "huge.txt", "type": "blob", "size": 999_999},
    ]
}


def fake_github(file_bytes: bytes = b"print('hi')\n") -> httpx.Client:
    """A stand-in for GitHub: knows one repo, a small file tree, and a few files."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/octo/demo":
            return httpx.Response(200, json=REPO_INFO)
        if path == "/repos/octo/missing":
            return httpx.Response(404, json={"message": "Not Found"})
        if path == "/repos/octo/demo/git/trees/main":
            return httpx.Response(200, json=TREE)
        if path == "/repos/octo/demo/contents/app/main.py":
            return httpx.Response(200, content=file_bytes)
        if path == "/repos/octo/demo/contents/assets/logo.png":
            return httpx.Response(200, content=b"\x89PNG\x00\x00binary")
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.Client(base_url=GITHUB_API, transport=httpx.MockTransport(handler), headers={})


def make_tools(**kwargs) -> RepoTools:
    return RepoTools("octo", "demo", http=fake_github(**kwargs))


# ---------- URL parsing ----------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/octo/demo",
        "github.com/octo/demo",
        "https://github.com/octo/demo.git",
        "https://github.com/octo/demo/tree/main/app",
        "  https://www.github.com/octo/demo/  ",
    ],
)
def test_parse_repo_url_accepts_common_forms(url):
    assert parse_repo_url(url) == ("octo", "demo")


@pytest.mark.parametrize("url", ["https://gitlab.com/octo/demo", "not a url", ""])
def test_parse_repo_url_rejects_non_github(url):
    with pytest.raises(RepoNotFoundError):
        parse_repo_url(url)


# ---------- get_repo_info ----------


def test_get_repo_info_summarises_the_repo():
    text = make_tools().get_repo_info()
    assert "octo/demo" in text
    assert "Python" in text
    assert "agents, demo" in text
    assert "MIT License" in text
    assert "Default branch: main" in text


def test_missing_repo_is_a_clear_error():
    tools = RepoTools("octo", "missing", http=fake_github())
    text, is_error = tools.run("get_repo_info", {})
    assert is_error
    assert "not found" in text


# ---------- list_files ----------


def test_list_root_shows_folders_with_counts_and_files_with_sizes():
    text = make_tools().list_files("")
    assert "app/  (3 files)" in text  # counts files nested below it
    assert "assets/  (1 files)" in text
    assert "README.md  (120 B)" in text


def test_list_subfolder_only_shows_its_direct_contents():
    text = make_tools().list_files("app")
    assert "utils/  (2 files)" in text
    assert "main.py  (300 B)" in text
    assert "helpers.py" not in text  # that one is one level deeper


def test_list_missing_folder_is_an_error():
    text, is_error = make_tools().run("list_files", {"path": "nope"})
    assert is_error
    assert "No folder named 'nope'" in text


def test_list_accepts_messy_paths():
    assert "main.py" in make_tools().list_files("/app/")
    assert "main.py" in make_tools().list_files("./app")


# ---------- search_code ----------


def test_search_without_token_matches_file_paths_only():
    text = make_tools().search_code("utils")
    assert "app/utils/helpers.py" in text
    assert "file paths only" in text


def test_search_with_no_hits_says_so():
    text = make_tools().search_code("zzz-nothing")
    assert text.startswith("No matches for 'zzz-nothing'")


def test_search_rejects_empty_query():
    text, is_error = make_tools().run("search_code", {"query": "   "})
    assert is_error
    assert "empty" in text


# ---------- read_file ----------


def test_read_file_returns_numbered_lines_and_records_the_citation():
    tools = make_tools(file_bytes=b"a = 1\nb = 2\nc = 3\n")
    text = tools.read_file("app/main.py")
    assert "app/main.py (lines 1-3 of 3)" in text
    assert "    1  a = 1" in text
    assert "    3  c = 3" in text
    assert tools.files_read == ["app/main.py"]


def test_read_file_long_file_tells_claude_how_to_continue():
    body = "\n".join(f"line {i}" for i in range(1, 1001)).encode()
    text = make_tools(file_bytes=body).read_file("app/main.py")
    assert "lines 1-400 of 1000" in text
    assert "start_line=401" in text


def test_read_file_continues_from_start_line():
    body = "\n".join(f"line {i}" for i in range(1, 1001)).encode()
    text = make_tools(file_bytes=body).read_file("app/main.py", start_line=995)
    assert "lines 995-1000 of 1000" in text
    assert "More remains" not in text


def test_read_file_refuses_binary_files():
    text, is_error = make_tools().run("read_file", {"path": "assets/logo.png"})
    assert is_error
    assert "binary" in text


def test_read_file_refuses_huge_files_without_downloading_them():
    text, is_error = make_tools().run("read_file", {"path": "huge.txt"})
    assert is_error
    assert "too large" in text


def test_read_file_unknown_path_is_an_error():
    text, is_error = make_tools().run("read_file", {"path": "ghost.py"})
    assert is_error
    assert "No file named 'ghost.py'" in text


# ---------- dispatcher ----------


def test_run_unknown_tool_is_an_error_not_a_crash():
    text, is_error = make_tools().run("delete_everything", {})
    assert is_error
    assert "Unknown tool" in text


def test_run_survives_network_failure():
    def boom(request):
        raise httpx.ConnectError("no network")

    tools = RepoTools("octo", "demo", http=httpx.Client(transport=httpx.MockTransport(boom)))
    text, is_error = tools.run("get_repo_info", {})
    assert is_error
    assert "Error talking to GitHub" in text


def test_tool_definitions_have_names_and_schemas():
    from app.tools import TOOL_DEFINITIONS

    names = {t["name"] for t in TOOL_DEFINITIONS}
    assert names == {"get_repo_info", "list_files", "search_code", "read_file"}
    for tool in TOOL_DEFINITIONS:
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"
