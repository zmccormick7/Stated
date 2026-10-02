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
  if (block.type === "chart") return renderChart(block);
  return el("p", { class: "note", text: `(${block.type} block)` });
}

const number = (value) => Number.isInteger(value) ? value.toLocaleString("en-US")
  : value.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const SHADES = ["#1A1A1A", "#6B6B6B", "#A3A3A3", "#C9C9C9"];  // Parts in fixed order; the remainder is outlined.

// Horizontal bars, one row per entry; several parts stack. Width is a share of `whole` when the chart
// has one, else of the largest row. Labels sit inside a segment when it is wide enough; every segment
// carries its full label and value as a tooltip.
function renderChart(block) {
  const figure = el("figure", { class: "chart" + (block.lead ? " lead" : "") });
  const labels = [...new Set(block.rows.flatMap((row) => row.parts.map((part) => part.label)))];
  const totals = block.rows.map((row) => row.parts.reduce((sum, part) => sum + part.value, 0));
  const scale = block.whole || Math.max(...totals, 0) || 1;
  if (labels.length > 1) figure.append(el("div", { class: "legend" }, labels.map((label, i) =>
    el("span", {}, [el("i", { style: `background:${SHADES[i % SHADES.length]}` }), el("span", { text: label })]))));
  const grid = el("div", { class: "rows" });
  block.rows.forEach((row, r) => {
    const bar = el("div", { class: "bar" });
    row.parts.forEach((part) => {
      const share = Math.max(0, part.value) / scale;
      const shade = labels.indexOf(part.label) % SHADES.length;
      const segment = el("div", { class: "part", title: `${row.label} · ${part.label}: ${number(part.value)} ${block.unit}`,
        style: `width:${(share * 100).toFixed(2)}%;background:${SHADES[shade]};color:${shade < 2 ? "#FFFFFF" : "#1A1A1A"}` });
      if (share >= 0.07) segment.append(el("span", { text: number(part.value) }));
      bar.append(segment);
    });
    if (block.whole) {
      const rest = block.whole - totals[r];
      if (rest > 0) bar.append(el("div", { class: "part rest", title: `${row.label} · ${block.rest_label || "Rest"}: ${number(rest)} ${block.unit}`,
                                           style: `width:${(rest / scale * 100).toFixed(2)}%` }, [el("span", { text: block.rest_label || "" })]));
    } else bar.append(el("b", { text: number(totals[r]) }));
    grid.append(el("span", { class: "label", text: row.label }), bar);
  });
  figure.append(grid, el("figcaption", { text: [block.caption, block.unit].filter(Boolean).join(" · ") }));
  return figure;
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
