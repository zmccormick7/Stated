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
    const from = source.cited ? source.cited.title : `${source.kind} ${source.record.slice(0, 8)}`;
    return el("blockquote", {}, [el("p", { text: `“${source.quote}”` }), el("small", { text: `${block.source} · ${from}` })]);
  }
  if (block.type === "chart") return renderChart(block);
  if (block.type === "lever") return renderLever(block);
  if (block.type === "flow") return renderFlow(block);
  return el("p", { class: "note", text: `(${block.type} block)` });
}

const number = (value) => Number.isInteger(value) ? value.toLocaleString("en-US")
  : value.toLocaleString("en-US", { maximumFractionDigits: Math.abs(value) < 10 ? 2 : 1 });
const SHADES = ["#1A1A1A", "#6B6B6B", "#A3A3A3", "#C9C9C9"];  // Parts in fixed order; the remainder is outlined.

// Horizontal bars, one row per entry; several parts stack. Width is a share of `whole` when the chart
// has one, else of the largest row. Labels sit inside a segment when it is wide enough; every segment
// carries its full label and value as a tooltip.
function renderChart(block) {
  const figure = el("figure", { class: "chart" + (block.lead ? " lead" : "") });
  const labels = [...new Set(block.rows.flatMap((row) => row.parts.map((part) => part.label)))];
  const totals = block.rows.map((row) => row.parts.reduce((sum, part) => sum + part.value, 0));
  const scale = block.whole || Math.max(...totals, 0) || 1;
  const plain = block.rows.every((row) => row.parts.length === 1);  // One bar per row: the row label names it.
  if (labels.length > 1 && !plain) figure.append(el("div", { class: "legend" }, labels.map((label, i) =>
    el("span", {}, [el("i", { style: `background:${SHADES[i % SHADES.length]}` }), el("span", { text: label })]))));
  const grid = el("div", { class: "rows" });
  block.rows.forEach((row, r) => {
    const bar = el("div", { class: "bar" });
    row.parts.forEach((part) => {
      const share = Math.max(0, part.value) / scale;
      const shade = labels.indexOf(part.label) % SHADES.length;
      const segment = el("div", { class: "part", title: `${row.label} · ${part.label}: ${number(part.value)} ${block.unit}`,
        style: `width:${(share * 100).toFixed(2)}%;background:${SHADES[shade]};color:${shade < 2 ? "#FFFFFF" : "#1A1A1A"}` });
      if (share >= 0.07 && !(plain && !block.whole)) segment.append(el("span", { text: number(part.value) }));
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

// A value in the Variable's units: fractions read as percentages, everything else as a plain number.
const inUnits = (value, units) => (units || "").startsWith("fraction") ? `${number(value * 100)}%` : number(value);

// One input to drag and one output to watch, recomputed through the model on the server as the slider moves.
function renderLever(block) {
  const { input, output } = block;
  const out = el("div", { class: "value", text: inUnits(output.value, output.units) });
  const base = el("div", { class: "base", text: "base case" });
  const label = el("label", { for: `lever-${input.key}`, text: `${input.label}: ${inUnits(input.value, input.units)}` });
  const slider = el("input", { id: `lever-${input.key}`, type: "range", min: input.low, max: input.high,
                               step: (input.high - input.low) / 200, value: input.value });
  const back = el("button", { type: "button", class: "back", text: `Back to ${inUnits(input.value, input.units)}` });
  let pending = null;
  const move = (value) => {
    label.textContent = `${input.label}: ${inUnits(value, input.units)}`;
    clearTimeout(pending);
    pending = setTimeout(async () => {
      try {
        const result = await api(`/api/models/${block.model}/evaluate`, { overrides: { [input.key]: value } });
        const now = result.values[output.key];
        out.textContent = inUnits(now, output.units);
        const delta = now - output.value;
        base.textContent = Math.abs(delta) < 1e-9 ? "base case"
          : `base case ${inUnits(output.value, output.units)} · ${delta > 0 ? "+" : "−"}${inUnits(Math.abs(delta), output.units)}`;
      } catch (error) { base.textContent = error.message; }
    }, 120);
  };
  slider.oninput = () => move(parseFloat(slider.value));
  back.onclick = () => { slider.value = input.value; move(input.value); };
  return el("figure", { class: "lever" + (block.lead ? " lead" : "") }, [
    el("div", { class: "head" }, [el("div", {}, [el("div", { class: "muted", text: output.label }), out]), base]),
    el("div", { class: "control" }, [el("div", { class: "row" }, [label, back]), slider]),
    el("figcaption", { text: block.caption || "" })]);
}

const node = (spec, cls) => el("div", { class: `node ${cls}` }, [
  el("div", { class: "title", text: spec.label }),
  ...(spec.note ? [el("div", { class: "note", text: spec.note })] : []),
  ...(spec.items || []).map((item) => el("div", { class: "item", text: item }))]);

// A source on the left, parts stacked in proportion in the middle, and an optional destination on the
// right. Heights are shares of `whole`, or of the parts' sum when there is none.
function renderFlow(block) {
  const total = block.parts.reduce((sum, part) => sum + Math.max(0, part.value), 0);
  const scale = block.whole || total || 1;
  const show = block.whole === 1 ? (value) => `${number(value * 100)}%` : number;  // Shares of one read as percentages.
  const column = el("div", { class: "column" });
  block.parts.forEach((part, i) => {
    const share = Math.max(0, part.value) / scale, shade = i % SHADES.length;
    const segment = el("div", { class: "part", title: `${part.label}: ${show(part.value)} ${block.unit || ""}`,
      style: `flex-grow:${share.toFixed(4)};background:${SHADES[shade]};color:${shade < 2 ? "#FFFFFF" : "#1A1A1A"}` }, [
      el("span", { class: "name", text: part.label + (part.note && share >= 0.2 ? ` · ${part.note}` : "") }),
      el("b", { text: show(part.value) })]);
    column.append(segment);
  });
  if (block.whole && block.whole - total > 0) {
    const rest = block.whole - total;
    column.append(el("div", { class: "part rest", title: `${block.rest_label || "Rest"}: ${show(rest)} ${block.unit || ""}`,
      style: `flex-grow:${(rest / scale).toFixed(4)}` }, [el("span", { class: "name", text: block.rest_label || "" }), el("b", { text: show(rest) })]));
  }
  const body = el("div", { class: "lanes" }, [node(block.from, "from"), el("div", { class: "link" }), column]);
  if (block.to) body.append(el("div", { class: "link dashed" }), node(block.to, "to"));
  return el("figure", { class: "flow" + (block.lead ? " lead" : "") }, [
    body, el("figcaption", { text: [block.caption, block.unit].filter(Boolean).join(" · ") })]);
}

// A source entry: what the record says, in a line, opening to the record itself.
function renderSource(source, i) {
  const text = source.text || `${source.kind} ${source.record.slice(0, 8)}`;
  const head = text.length > 150 ? text.slice(0, 149) + "…" : text;
  const body = el("div", { class: "record" });
  const details = el("details", {}, [el("summary", { text: head }), body]);
  details.ontoggle = () => { if (details.open && !body.hasChildNodes()) openRecord(body, source.record, []); };
  return el("li", { id: `source-${i + 1}` }, [details]);
}

// Fetch a record into a container and draw it: text, what it rests on, and one hop of links, each of
// which opens in the same place. `trail` is the records opened before it, for the Back link.
async function openRecord(container, recordId, trail) {
  container.replaceChildren(el("p", { class: "muted", text: "Loading…" }));
  let record;
  try { record = await api(`/api/records/${recordId}`); } catch (error) {
    return container.replaceChildren(el("p", { class: "muted", text: error.message }));
  }
  const attrs = record.attributes || {};
  const facts = [record.kind, attrs.basis, attrs.status, attrs.key && `key ${attrs.key}`,
                 attrs.value !== undefined && `value ${JSON.stringify(attrs.value)}`, attrs.formula && `formula ${attrs.formula}`,
                 record.layer && `${record.layer} layer`, record.author_id].filter(Boolean).join(" · ");
  const nodes = [el("p", { text: record.text }), el("p", { class: "muted", text: facts })];
  for (const citation of record.citations || [])
    nodes.push(el("blockquote", {}, [el("p", { text: `“${citation.quote}”` }), el("small", { text: citation.title || "" })]));
  if ((record.connections || []).length) nodes.push(el("ul", { class: "links" }, record.connections.map((c) => {
    const chip = el("button", { type: "button", text: `${c.record.kind}: ${c.record.text}` });
    chip.onclick = () => openRecord(container, c.record.id, [...trail, record]);
    return el("li", {}, [el("span", { class: "muted", text: c.direction === "out" ? `${c.relation} →` : `← ${c.relation}` }), chip]);
  })));
  if (trail.length) {
    const back = el("button", { type: "button", class: "back", text: `Back to ${trail[trail.length - 1].kind}` });
    back.onclick = () => openRecord(container, trail[trail.length - 1].id, trail.slice(0, -1));
    nodes.unshift(back);
  }
  container.replaceChildren(...nodes);
}

function renderAnswer(answer) {
  const sources = answer.sources || [];
  const node = el("div", { class: "answer" }, [el("h1", { text: answer.title }), cited(answer.body, sources)]);
  for (const block of answer.blocks || []) node.append(renderBlock(block, sources));
  if (sources.length) node.append(el("ol", { class: "sources" }, sources.map(renderSource)));
  return node;
}

function renderTurn(turn) {
  const node = el("div", { class: "turn" }, [el("div", { class: "question", text: turn.question })]);
  if (turn.status === "running") {
    const last = turn.progress[turn.progress.length - 1];
    node.append(el("div", { class: "progress", text: last ? `${last}…` : "Starting…" }));
  } else if (turn.status === "done") {
    node.append(renderAnswer(turn.answer));
  } else {  // Refused tools are shown only here, where they may explain the failure.
    const denied = (turn.denied || []).length ? ` Refused under your settings: ${turn.denied.join(", ")}.` : "";
    node.append(el("div", { class: "error", text: (turn.error || "The answer failed.") + denied }));
  }
  const written = Object.entries(turn.written || {}).map(([kind, n]) => `${n} ${kind}${n === 1 ? "" : "s"}`);
  if (written.length) node.append(el("div", { class: "note", text: `Added to your layer: ${written.join(", ")}` }));
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
