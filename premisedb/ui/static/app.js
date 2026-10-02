// The answer UI: a sidebar of threads and the current thread's turns, polled while a turn is running.
const state = { threads: [], current: null, thread: null };
const $ = (id) => document.getElementById(id);
const api = async (path, body) => {
  const response = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" },
                                              body: JSON.stringify(body) } : {});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
};
const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) key === "text" ? node.textContent = value : node.setAttribute(key, value);
  node.append(...children);
  return node;
};
const escape = (text) => text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// Text with [n] markers becomes a paragraph whose markers link to the sources list.
function cited(text, sources) {
  const node = el("p");
  node.innerHTML = escape(text).replace(/\[(\d+)\]/g, (match, n) =>
    n >= 1 && n <= sources.length ? `<sup><a href="#source-${n}">${n}</a></sup>` : match);
  return node;
}

function renderBlock(block, sources) {
  if (block.type === "text") return cited(block.text, sources);
  if (block.type === "quote") {
    const source = sources[block.source - 1];
    return el("blockquote", {}, [el("p", { text: `“${source.quote}”` }),
                                 el("small", { text: `${block.source} · ${source.kind} ${source.record.slice(0, 8)}` })]);
  }
  if (block.type === "chart") {  // Drawn as figures until the chart renderer lands.
    const grid = el("div", { class: "figures" });
    for (const row of block.rows) for (const part of row.parts)
      grid.append(el("span", { text: row.label === part.label ? part.label : `${row.label} · ${part.label}` }),
                  el("b", { text: `${part.value} ${block.unit}` }));
    return grid;
  }
  return el("p", { class: "note", text: `(${block.type} block)` });
}

function renderAnswer(answer) {
  const sources = answer.sources || [];
  const node = el("div", { class: "answer" }, [el("h1", { text: answer.title }), cited(answer.body, sources)]);
  for (const block of answer.blocks || []) node.append(renderBlock(block, sources));
  if (sources.length) node.append(el("ol", { class: "sources" }, sources.map((source, i) =>
    el("li", { id: `source-${i + 1}`, text: `${source.kind} ${source.record.slice(0, 8)}` + (source.quote ? ` — “${source.quote}”` : "") }))));
  return node;
}

function renderTurn(turn) {
  const node = el("div", { class: "turn" }, [el("div", { class: "question", text: turn.question })]);
  if (turn.status === "running") {
    const last = turn.progress[turn.progress.length - 1];
    node.append(el("div", { class: "progress", text: last ? `${last}…` : "Starting…" }));
  } else if (turn.status === "done") {
    node.append(renderAnswer(turn.answer));
  } else {
    node.append(el("div", { class: "error", text: turn.error || "The answer failed." }));
  }
  const written = Object.entries(turn.written || {}).map(([kind, n]) => `${n} ${kind}${n === 1 ? "" : "s"}`);
  if (written.length) node.append(el("div", { class: "note", text: `Added to your layer: ${written.join(", ")}` }));
  if ((turn.denied || []).length) node.append(el("div", { class: "note", text: `Refused under your settings: ${turn.denied.join(", ")}` }));
  return node;
}

function render() {
  const list = $("threads");
  list.replaceChildren(...state.threads.map((thread) => {
    const item = el("li", { class: thread.id === state.current ? "current" : "", text: thread.title, title: thread.title });
    item.onclick = () => select(thread.id);
    return item;
  }));
  const main = $("thread");
  if (!state.thread) main.replaceChildren(el("div", { class: "empty", text: "Ask a question about the graph." }));
  else main.replaceChildren(...state.thread.turns.map(renderTurn));
  $("question").placeholder = state.thread ? "Ask a follow-up" : "Ask a question";
}

async function refresh() {
  state.threads = await api("/api/threads");
  if (state.current) state.thread = await api(`/api/threads/${state.current}`);
  render();
  if (state.thread && state.thread.turns.some((turn) => turn.status === "running")) setTimeout(refresh, 1500);
}

async function select(threadId) {
  state.current = threadId;
  await refresh();
  $("main").scrollTop = $("main").scrollHeight;
}

$("ask").onsubmit = async (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question) return;
  try {
    const started = await api(state.current ? `/api/threads/${state.current}/turns` : "/api/threads", { question });
    $("question").value = "";
    await select(started.thread_id);
  } catch (error) {
    alert(error.message);
  }
};
$("new").onclick = () => { state.current = null; state.thread = null; render(); $("question").focus(); };
$("toggle").onclick = () => $("sidebar").classList.toggle("collapsed");
refresh();
