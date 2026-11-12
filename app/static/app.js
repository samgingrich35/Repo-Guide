// Repo Guide front end: talks to the two API routes and draws the conversation.
// Plain JavaScript, no build step.

const $ = (id) => document.getElementById(id);
let sessionId = null;

// ---------- API calls ----------

async function api(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    // FastAPI puts the reason in "detail" (a string, or a list for validation errors).
    const detail = Array.isArray(data.detail) ? data.detail[0]?.msg : data.detail;
    throw new Error(detail || `Request failed (HTTP ${resp.status})`);
  }
  return data;
}

// ---------- starting a repo ----------

$("start-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const url = $("repo-url").value.trim();
  if (!url) return;

  $("start-error").hidden = true;
  setBusy(true);
  $("hero").hidden = true;
  $("chat").hidden = false;
  $("thread").replaceChildren();
  setRepo(url.replace(/^https?:\/\/(www\.)?github\.com\//, "").replace(/\/$/, ""));

  const working = showWorking("Exploring the repo: reading its structure and key files");
  try {
    const data = await api("/api/sessions", { repo_url: url });
    sessionId = data.session_id;
    setRepo(data.repo);
    working.done();
    showAnswer(data.overview);
    $("question").focus();
  } catch (err) {
    working.done();
    $("chat").hidden = true;
    $("hero").hidden = false;
    $("start-error").textContent = err.message;
    $("start-error").hidden = false;
  } finally {
    setBusy(false);
  }
});

document.querySelectorAll(".chip").forEach((chip) =>
  chip.addEventListener("click", () => {
    $("repo-url").value = chip.dataset.url;
    $("start-form").requestSubmit();
  })
);

$("new-btn").addEventListener("click", () => {
  sessionId = null;
  $("chat").hidden = true;
  $("hero").hidden = false;
  $("repo-url").value = "";
  $("repo-url").focus();
});

// ---------- asking questions ----------

$("ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question || !sessionId) return;

  $("question").value = "";
  autoGrow();
  showUser(question);
  setBusy(true);
  const working = showWorking("Thinking and exploring");
  try {
    const data = await api(`/api/sessions/${sessionId}/messages`, { message: question });
    working.done();
    showAnswer(data);
  } catch (err) {
    working.done();
    showError(err.message);
  } finally {
    setBusy(false);
    $("question").focus();
  }
});

// Enter sends, Shift+Enter makes a new line.
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("ask-form").requestSubmit();
  }
});
$("question").addEventListener("input", autoGrow);

function autoGrow() {
  const box = $("question");
  box.style.height = "auto";
  box.style.height = `${box.scrollHeight}px`;
}

// ---------- drawing the conversation ----------

function setRepo(fullName) {
  $("repo-name").textContent = fullName;
  $("repo-name").href = `https://github.com/${fullName}`;
}

function setBusy(busy) {
  for (const id of ["start-btn", "ask-btn", "question"]) $(id).disabled = busy;
}

function fromTemplate(id) {
  return $(id).content.firstElementChild.cloneNode(true);
}

function addToThread(node) {
  $("thread").append(node);
  node.scrollIntoView({ behavior: "smooth", block: "start" });
  return node;
}

function showUser(text) {
  const node = fromTemplate("tpl-user");
  node.querySelector(".bubble").textContent = text;
  addToThread(node);
}

// A spinner with a seconds counter, because agent answers can take a while.
function showWorking(label) {
  const node = addToThread(fromTemplate("tpl-working"));
  const status = node.querySelector(".status");
  const started = Date.now();
  const tick = () => {
    const secs = Math.round((Date.now() - started) / 1000);
    status.textContent = `${label}… ${secs}s`;
  };
  tick();
  const timer = setInterval(tick, 1000);
  return { done: () => { clearInterval(timer); node.remove(); } };
}

function showAnswer(data) {
  const node = fromTemplate("tpl-agent");

  // Answer: Markdown → HTML → sanitized, so a malicious repo can't inject scripts.
  node.querySelector(".answer").innerHTML = DOMPurify.sanitize(marked.parse(data.answer));

  // Sources: the files the agent actually opened, linked to GitHub.
  if (data.files_read.length) {
    const list = node.querySelector(".sources ul");
    for (const file of data.files_read) {
      const a = document.createElement("a");
      a.href = file.url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = file.path;
      const li = document.createElement("li");
      li.append(a);
      list.append(li);
    }
    node.querySelector(".sources").hidden = false;
  }

  // Trace: every tool call, so you can watch how the agent worked it out.
  const n = data.steps.length;
  const cost = data.cost_usd == null ? "" : ` · ~$${data.cost_usd.toFixed(4)}`;
  const tokens = (data.input_tokens + data.output_tokens).toLocaleString();
  node.querySelector(".trace summary").textContent =
    `How I found this: ${n} step${n === 1 ? "" : "s"} · ${tokens} tokens${cost}`;
  const steps = node.querySelector(".steps");
  for (const step of data.steps) {
    const li = document.createElement("li");
    li.textContent = `${step.tool}(${formatInput(step.input)})${step.is_error ? "  → error" : ""}`;
    if (step.is_error) li.className = "err";
    steps.append(li);
  }
  if (!n) node.querySelector(".trace").hidden = true;

  addToThread(node);
}

function formatInput(input) {
  return Object.entries(input)
    .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
    .join(", ");
}

function showError(message) {
  const node = document.createElement("div");
  node.className = "msg agent error-msg";
  node.setAttribute("role", "alert");
  node.textContent = message;
  addToThread(node);
}
