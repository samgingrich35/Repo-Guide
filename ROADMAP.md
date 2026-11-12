# Repo Guide: Build Roadmap

This is the plain-English map of how the project gets built. Each step ends with something you can see working before moving on.

```
 YOU ──paste URL / ask──▶ WEB PAGE ──▶ SERVER ──▶ AGENT LOOP ──▶ ANSWER
                                                    │   ▲
                                         "use tool" │   │ result
                                                    ▼   │
                                                  TOOLS ──▶ GitHub
```

| Step | What we build | In plain English | How you'll know it works | Status |
|---|---|---|---|---|
| 0 | Setup | Make the project folder, install Python tools, start git | Folder exists, `git status` works | ✅ |
| 1 | Skeleton app | A tiny web server that just says "I'm alive" | `localhost:8000/health` → `{"status":"ok"}` | ✅ |
| 2 | Tools (the agent's "hands") | 4 functions that fetch from GitHub: repo info, file list, search, read file | Tests pass, and a real repo's files print out | ⬜ |
| 3 | Agent loop (the "brain") | Ask Claude "which tool next?" → run it → send result back → repeat → answer | Tests pass with a fake Claude (no API cost) | ⬜ |
| 4 | Overview + chat | Auto-describe the repo, then follow-up questions with memory | Ask about a real repo from the command line | ⬜ |
| 5 | Web page | URL box, overview, chat, and a "files it read" panel | Use it in the browser | ⬜ |
| 6 | Safety + quality | Step limit, rate limit, cost tracking, lint + tests | `ruff` and `pytest` all green | ⬜ |
| 7 | Docker | Put the app in a "box" that runs the same anywhere | `docker run` works locally | ⬜ |
| 8 | CI/CD | GitHub tests every change and deploys when tests pass | Green checkmark on GitHub | ⬜ |
| 9 | Go live | Host it on Render with a public link | Public URL works | ⬜ |
| 10 | Show it off | README, diagram, demo GIF, "how I built it with Claude Code" | Repo looks professional | ⬜ |

**Where you need to act:** Step 4 (Anthropic API key), Step 7 (install Docker Desktop), Steps 8–9 (create the GitHub repo + Render account).

## Key words
- **Agent:** an AI that decides for itself which actions to take, in a loop, until the job is done.
- **Tool:** a function the AI is allowed to call (here: read files from GitHub).
- **API:** a way for programs to talk to each other over the internet.
- **CI/CD:** robots that automatically test (CI) and ship (CD) your code on every change.
- **Docker:** packages the app plus everything it needs into one portable "container."
