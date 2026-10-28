// The answer UI: a sidebar of threads and the current thread's turns, polled while a turn is running.
const state = { threads: [], current: null, thread: null, status: null,
                view: "threads", coverage: [], feature: null, series: null, sort: { key: "subject", desc: false }, onlyNull: false };
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
const RUNS = { 60: "1 hour", 180: "3 hours", 480: "8 hours" };  // Efforts that are research runs, which have agents.

// Patch `node` so it matches `target`, touching only what differs, so a page that polls keeps what the
// reader did to it: open folds, a text selection, the scroll of a log. Children with data-key are matched
// by key; a fold's open state belongs to the reader and is left alone.
function morph(node, target) {
  if (node.nodeType !== target.nodeType || node.nodeName !== target.nodeName) return node.replaceWith(target);
  if (node.nodeType === Node.TEXT_NODE) { if (node.data !== target.data) node.data = target.data; return; }
  for (const { name, value } of [...target.attributes]) if (name !== "open" && node.getAttribute(name) !== value) node.setAttribute(name, value);
  for (const { name } of [...node.attributes]) if (name !== "open" && !target.hasAttribute(name)) node.removeAttribute(name);
  for (const key of ["onclick", "ontoggle", "oninput", "onchange"]) if (target[key]) node[key] = target[key];
  const keyed = new Map([...node.childNodes].filter((c) => c.dataset && c.dataset.key).map((c) => [c.dataset.key, c]));
  const have = [...node.childNodes], want = [...target.childNodes];
  want.forEach((child, i) => {
    const key = child.dataset && child.dataset.key;
    const match = key && keyed.has(key) ? keyed.get(key) : (have[i] && !(have[i].dataset && have[i].dataset.key) ? have[i] : null);
    if (!match) return node.insertBefore(child, node.childNodes[i] || null);
    if (node.childNodes[i] !== match) node.insertBefore(match, node.childNodes[i] || null);
    morph(match, child);
  });
  while (node.childNodes.length > want.length) node.lastChild.remove();
}
const escape = (text) => text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// Text with [n] markers becomes paragraphs (split on blank lines) whose markers link to the sources list.
function cited(text, sources) {
  const node = el("div", { class: "prose" });
  for (const part of text.split(/\n\s*\n/).filter((p) => p.trim())) {
    const paragraph = el("p");
    paragraph.innerHTML = escape(part.trim()).replace(/\[(\d+)\]/g, (match, n) =>
      n >= 1 && n <= sources.length ? `<sup><a href="#source-${n}">${n}</a></sup>` : match);
    node.append(paragraph);
  }
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

// Against a feature value's date: when a cutoff first sees a piece of its evidence. Evidence seen only
// after that date, or never (a Source with no publication date), could not have been read on it.
function seenNote(seen, asOf) {
  if (!asOf || seen === "" || seen === undefined) return [];
  if (seen === null) return [el("span", { class: "seen late", text: "no publication date, so no cutoff sees it" })];
  return [el("span", { class: seen > asOf ? "seen late" : "seen muted", text: seen > asOf ? `seen from ${seen}, after ${asOf}` : `seen from ${seen}` })];
}

// Fetch a record into a container and draw it: text, what it rests on, and one hop of links, each of
// which opens in the same place. `trail` is the records opened before it, for the Back link. With `asOf`,
// the date of the feature value the trail started from, what the record rests on is checked against it.
async function openRecord(container, recordId, trail, asOf) {
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
    nodes.push(el("blockquote", {}, [el("p", { text: `“${citation.quote}”` }),
                                     el("small", {}, [el("span", { text: citation.title || "" }), ...seenNote(citation.seen_from, asOf)])]));
  if ((record.connections || []).length) nodes.push(el("ul", { class: "links" }, record.connections.map((c) => {
    const chip = el("button", { type: "button", text: `${c.record.kind}: ${c.record.text}` });
    chip.onclick = () => openRecord(container, c.record.id, [...trail, record], asOf);
    // Only what the record rests on (its outgoing links) is checked; later records may point back at it.
    const seen = c.direction === "out" ? seenNote(c.record.seen_from, asOf) : [];
    return el("li", {}, [el("span", { class: "muted", text: c.direction === "out" ? `${c.relation} →` : `← ${c.relation}` }),
                         seen.length ? el("div", { class: "target" }, [chip, ...seen]) : chip]);
  })));
  if (trail.length) {
    const back = el("button", { type: "button", class: "back", text: `Back to ${trail[trail.length - 1].kind}` });
    back.onclick = () => openRecord(container, trail[trail.length - 1].id, trail.slice(0, -1), asOf);
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

// A research run's time, in the user's terms: "1 h 12 min of 3 hours".
function used(turn) {
  const end = turn.finished_at ? new Date(turn.finished_at) : new Date();
  const minutes = Math.max(0, Math.round((end - new Date(turn.started_at)) / 60000));
  const text = minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes} min`;
  return { minutes, text };
}

// The lists of a run's pieces of work: the first few shown, the rest behind "Show all".
function pieces(title, items, shown = 4) {
  // Each piece by its title; its detail opens on click, and a piece in hand shows its worker's latest step.
  const row = (item) => el("details", { class: "piece", "data-key": String(item.id) }, [el("summary", { text: item.title || item.text }),
    ...(item.status === "running" && item.progress ? [el("div", { class: "note", text: item.progress })] : []),
    ...(item.title && item.text !== item.title ? [el("div", { class: "detail", text: item.text })] : [])]);
  const section = el("section", { class: "pieces", "data-key": title }, [el("h2", { text: title }), ...items.slice(0, shown).map(row)]);
  if (items.length > shown) {
    section.append(el("details", {}, [el("summary", { text: `Show all ${items.length}` }), ...items.slice(shown).map(row)]));
  }
  return section;
}

// A research run while it goes: its state, time and counts in one box, then what is in hand, done and to do.
function renderRun(turn) {
  const items = turn.items, last = turn.progress[turn.progress.length - 1] || "";
  const running = items.filter((i) => i.status === "running"), done = items.filter((i) => i.status === "done").reverse();
  const todo = items.filter((i) => i.status === "todo"), added = Object.values(turn.written || {}).reduce((a, b) => a + b, 0);
  const time = `${used(turn).text} of ${RUNS[turn.effort]} · ${turn.agents === 1 ? "1 agent" : `${turn.agents} agents`}`;
  const waiting = last.startsWith("Waiting for your usage limit");
  const heading = turn.finish ? "Finishing: writing the report" : turn.paused ? "Paused"
    : waiting ? last : items.length ? "Researching" : "Planning the research";
  const sub = turn.paused ? `${time} · work in hand finishes, nothing new starts` : time;
  const control = async (action) => { try { await api(`/api/turns/${turn.id}/control`, { action }); await refresh(); } catch (e) { alert(e.message); } };
  const pause = el("button", { text: turn.paused ? "Resume" : "Pause" });
  pause.onclick = () => control(turn.paused ? "resume" : "pause");
  const finish = el("button", { text: "Finish now" });
  finish.onclick = () => control("finish");
  const count = (n, label) => el("div", { class: "count" }, [el("b", { text: String(n) }), el("span", { text: label })]);
  // The agent's latest step, so the run is never a closed box; the whole log folds out below the lists.
  const step = waiting ? "" : last;
  const box = el("section", { class: "run", "data-key": "run" }, [
    el("div", { class: "head" }, [el("div", {}, [el("div", { class: "state", text: heading }), el("div", { class: "muted", text: sub }),
                                                 ...(step ? [el("div", { class: "progress", text: `${step}…` })] : [])]),
                                  ...(turn.finish ? [] : [el("div", { class: "buttons" }, [pause, finish])])]),
    el("div", { class: "rule", role: "img", "aria-label": time }, [el("div", { style: `width: ${Math.min(100, used(turn).minutes / turn.effort * 100)}%` })]),
    el("div", { class: "counts" }, [count(done.length, "done"), count(running.length, "in progress"), count(todo.length, "to do"),
                                    count(added, "records added to your layer")]),
  ]);
  const node = [box];
  if (running.length) node.push(pieces("In progress", running, 10));
  if (done.length) node.push(pieces("Done", done));
  if (todo.length) node.push(pieces("To do", todo));
  const steps = turn.progress.length;
  node.push(el("details", { class: "log", "data-key": "log" }, [el("summary", { text: `Show all ${steps} step${steps === 1 ? "" : "s"}` }),
    el("div", { class: "lines" }, turn.progress.map((line) => el("div", { text: line })))]));
  return node;
}

// A finished run folds down to one line above its report.
function runSummary(turn) {
  const done = turn.items.filter((i) => i.status === "done");
  const added = Object.values(turn.written || {}).reduce((a, b) => a + b, 0);
  const summary = `Researched for ${used(turn).text} with ${turn.agents === 1 ? "1 agent" : `${turn.agents} agents`}: `
    + `${done.length} piece${done.length === 1 ? "" : "s"} of work, ${added} record${added === 1 ? "" : "s"} added to your layer`;
  return el("details", { class: "summary" }, [el("summary", { text: summary }),
    ...done.map((item) => el("div", { class: "piece" }, [el("div", { text: item.title || item.text }), el("div", { class: "note", text: item.result || "" })]))]);
}

function renderTurn(turn) {
  const node = el("div", { class: "turn" }, [el("div", { class: "question", text: turn.question })]);
  for (const note of turn.notes || []) {
    const at = new Date(note.created_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    node.append(el("div", { class: "question", text: `Added ${at}: ${note.text}` }));
  }
  const isRun = Boolean(RUNS[turn.effort]);
  if (turn.status === "running" && isRun) {
    node.append(...renderRun(turn));
  } else if (turn.status === "running") {
    const last = turn.progress[turn.progress.length - 1];
    node.append(el("div", { class: "progress", text: last ? `${last}…` : "Starting…" }));
  } else if (turn.status === "done") {
    if (isRun) node.append(runSummary(turn));
    node.append(renderAnswer(turn.answer));
  } else {  // Refused tools are shown only here, where they may explain the failure.
    const denied = (turn.denied || []).length ? ` Refused under your settings: ${turn.denied.join(", ")}.` : "";
    node.append(el("div", { class: "error", text: (turn.error || "The answer failed.") + denied }));
  }
  const written = Object.entries(turn.written || {}).map(([kind, n]) => `${n} ${kind}${n === 1 ? "" : "s"}`);
  if (written.length && !isRun) {  // A run shows its count in its box, then in its summary line.
    node.append(el("div", { class: "note", text: `Added to your layer: ${written.join(", ")}` }));
  }
  return node;
}

function render() {
  const list = $("threads");
  morph(list, el("ul", { id: "threads" }, state.threads.map((thread) => {
    const item = el("li", { class: thread.id === state.current ? "current" : "", text: thread.title, title: thread.title, "data-key": thread.id });
    item.onclick = () => select(thread.id);
    return item;
  })));
  const features = state.view !== "threads";
  $("main").classList.toggle("features", features);
  $("features-view").classList.toggle("current", features);
  if (features) $("features").replaceChildren(state.view === "feature" ? renderFeature(state.feature)
    : state.view === "series" ? renderSeries(state.series) : renderFeatures(state.coverage));
  const main = $("thread");
  $("main").classList.toggle("empty", !state.thread && !features);
  // A finished turn is left exactly as the reader has it (opened records, a moved lever); a running one is
  // patched in place as it changes, so the poll does not undo what the reader did.
  const existing = new Map([...main.children].map((node) => [node.dataset.key, node]));
  const turns = (state.thread ? state.thread.turns : []).map((turn) => {
    const key = String(turn.id), old = existing.get(key);
    if (old && old.dataset.status === turn.status && turn.status !== "running") return old;
    const fresh = renderTurn(turn);
    fresh.dataset.key = key;
    fresh.dataset.status = turn.status;
    if (old && turn.status === "running") { morph(old, fresh); return old; }
    return fresh;
  });
  if (turns.length !== main.children.length || turns.some((node, i) => node !== main.children[i])) main.replaceChildren(...turns);
  renderStatus();
  const run = runningRun();
  $("question").placeholder = run ? "Add a note to steer this research" : state.thread ? "Ask a follow-up" : "Ask a question";
  $("ask").classList.toggle("notes", Boolean(run));
  $("ask").querySelector("button").textContent = run ? "Add" : "Ask";
}

function runningRun() {
  return state.thread && state.thread.turns.find((turn) => turn.status === "running" && RUNS[turn.effort]);
}

// Status lives in the sidebar's foot; only a problem gets a sentence under the question box.
function renderStatus() {
  const status = state.status;
  $("status").textContent = "";
  $("problem").textContent = "";
  if (!status) return;
  const home = status.workspace.replace(/^\/Users\/[^/]+|^\/home\/[^/]+/, "~");
  const base = (status.layers || []).filter((layer) => !layer.writable).map((layer) => `${layer.name} ${layer.version}`);
  const agent = status.harness === "codex" ? "Codex" : "Claude Code";
  $("status").textContent = [home, ...base, agent].join(" · ");
  if (!base.length) $("problem").textContent = "No base layer is attached. Run stated ui --base FILE once.";
  else if (!status.harness_found) $("problem").textContent = `${agent} was not found. Install it and sign in, then ask.`;
}

// Features, read-only: every definition in the workspace, then one feature with its subjects' latest values.
const TYPES = { number: "Number", count: "Count", boolean: "Yes or no", category: "Category" };
const EVERY = { day: "Every day", weekday: "Every weekday", week: "Every week", month: "Every month", quarter: "Every quarter" };
const schedule = (d) => d.formula ? "Computed from other features" : d.every ? `${EVERY[d.every]} from ${d.start}` : "No schedule";
const plural = (n, word) => `${number(n)} ${word}${n === 1 ? "" : "s"}`;

// A value as the reader should see it: fractions as percentages, yes or no, and null marked as unknown.
function shown(value, units) {
  if (value === null || value === undefined) return el("span", { class: "muted", text: "null" });
  const text = typeof value === "boolean" ? (value ? "Yes" : "No") : typeof value === "number" ? inUnits(value, units)
    : Array.isArray(value) ? JSON.stringify(value) : String(value);
  return el("span", { text });
}

function grid(head, rows) {
  return el("div", { class: "scroll" }, [el("table", { class: "grid" }, [el("thead", {}, [el("tr", {}, head)]), el("tbody", {}, rows)])]);
}

function renderFeatures(coverage) {
  const node = el("div", { class: "features" }, [el("h1", { text: "Features" })]);
  if (!coverage.length) return node.append(el("p", { class: "muted",
    text: "No features yet. Ask for one in a thread, or define one with stated define-feature." })), node;
  node.append(grid(["Feature", "Type", "Schedule", "Subjects", "Latest value"].map((name, i) =>
    el("th", { class: i === 3 ? "num" : "", text: name })), coverage.map(({ definition: d, subjects, latest }) => {
    const open = el("button", { type: "button", class: "link", text: d.key });
    open.onclick = () => openFeature(d.id);
    return el("tr", {}, [el("td", {}, [open, ...(d.units ? [el("div", { class: "muted", text: d.units })] : [])]),
      el("td", { text: TYPES[d.type] }), el("td", { text: schedule(d) }), el("td", { class: "num", text: number(subjects) }),
      el("td", { class: "date", text: latest || "—" })]);
  })));
  return node;
}

// Sorting keeps nulls last whichever way it runs, so the values that are there stay together.
const SUBJECT_COLUMNS = [["subject", "Subject"], ["value", "Value"], ["as_of", "Changed"], ["checked", "Checked"], ["values", "Values"]];
function sortedSubjects(subjects) {
  const { key, desc } = state.sort;
  const known = subjects.filter((s) => s[key] !== null), unknown = subjects.filter((s) => s[key] === null);
  known.sort((a, b) => {
    const x = a[key], y = b[key];
    const order = typeof x === "string" && typeof y === "string" ? x.localeCompare(y) : Number(x) - Number(y);
    return desc ? -order : order;
  });
  return [...known, ...unknown];
}

function renderFeature(found) {
  const d = found.definition;
  const back = el("button", { type: "button", class: "link", text: "All features" });
  back.onclick = openFeatures;
  const range = d.categories ? d.categories.join(", ")
    : d.min !== null || d.max !== null ? `${d.min ?? "…"} to ${d.max ?? "…"}` : "";
  const facts = [TYPES[d.type], range, d.units, schedule(d), `written by ${d.author_id} on ${d.written_at.slice(0, 10)}`];
  const node = el("div", { class: "features" }, [back, el("h1", { text: d.key }),
    el("p", { class: "muted", text: facts.filter(Boolean).join(" · ") }), el("p", { text: d.instructions }),
    ...(d.formula ? [el("p", { class: "muted", text: `Formula: ${d.formula}` })] : [])]);
  if (d.replaced_by) {
    const newer = el("button", { type: "button", class: "link", text: "Open the current definition" });
    newer.onclick = () => openFeature(d.replaced_by);
    node.append(el("p", { class: "note" }, [el("span", { text: "Newer instructions replaced these. " }), newer]));
  }
  const values = found.subjects.reduce((sum, s) => sum + s.values, 0);
  const count = (n, label) => el("div", { class: "count" }, [el("b", { text: number(n) }), el("span", { text: label })]);
  node.append(el("div", { class: "stats" }, [count(found.subjects.length, "subjects with a value"), count(values, "values"),
    count(found.in_sample, "dated on or before the instructions"), ...(found.due === null ? [] : [count(found.due, "dates due")])]));
  const notes = [];
  if (found.in_sample) notes.push(`${plural(found.in_sample, "value")} ${found.in_sample === 1 ? "is" : "are"} dated on or before `
    + `${d.written_at.slice(0, 10)}, the day these instructions were written, so the instructions may carry hindsight about `
    + `${found.in_sample === 1 ? "it" : "them"}; only later dates test them honestly.`);
  if (found.due !== null) notes.push("Dates due counts only the subjects that already have a value.");
  if (notes.length) node.append(el("p", { class: "note", text: notes.join(" ") }));
  if (found.earlier.length) node.append(el("details", { class: "earlier" }, [
    el("summary", { text: `${plural(found.earlier.length, "earlier definition")} of ${d.key}` }),
    ...found.earlier.map((e) => {
      const open = el("button", { type: "button", class: "link", text: `Written ${e.written_at.slice(0, 10)}` });
      open.onclick = () => openFeature(e.id);
      return el("div", { class: "piece" }, [open, el("p", { text: e.instructions })]);
    })]));
  if (!found.subjects.length) return node.append(el("p", { class: "muted", text: "No subject has a value of this feature yet." })), node;
  const nulls = found.subjects.filter((s) => s.value === null).length;
  if (nulls) {
    const only = el("input", { type: "checkbox", id: "only-null" });
    only.checked = state.onlyNull;
    only.onchange = () => { state.onlyNull = only.checked; render(); };
    node.append(el("label", { class: "filter", for: "only-null" }, [only, el("span", { text: `Only the ${plural(nulls, "null")}` })]));
  }
  const head = SUBJECT_COLUMNS.map(([key, name]) => {
    const sorted = state.sort.key === key;
    const button = el("button", { type: "button", text: name + (sorted ? (state.sort.desc ? " ↓" : " ↑") : "") });
    button.onclick = () => { state.sort = { key, desc: sorted ? !state.sort.desc : false }; render(); };
    return el("th", { class: key === "values" ? "num" : "", "aria-sort": sorted ? (state.sort.desc ? "descending" : "ascending") : "none" }, [button]);
  });
  const rows = sortedSubjects(found.subjects.filter((s) => !state.onlyNull || s.value === null)).map((s) => el("tr", {}, [
    el("td", {}, [Object.assign(el("button", { type: "button", class: "link", text: s.subject }),
                                { onclick: () => openSeries(d.id, s.subject_id) })]), el("td", {}, [shown(s.value, d.units)]), el("td", { class: "date", text: s.as_of }),
    el("td", { class: "date", text: s.checked }), el("td", { class: "num", text: number(s.values) })]));
  node.append(grid(head, rows));
  return node;
}

// One subject's values over time as a step chart: each value holds until the next. Numbers and counts
// share a scale (the definition's bounds when it has them); yes/no and categories are lanes. A null leaves a
// gap. Dates up to the day the instructions were written are shaded, and checks are ticks along the axis.
function seriesChart(series) {
  const d = series.definition, byDate = new Map(series.points.map((p) => [p.as_of, p.value]));
  const points = [...byDate].map(([day, value]) => ({ day, value }));
  if (!points.length || points.some((p) => Array.isArray(p.value))) return null;
  const time = (day) => Date.parse(`${day}T00:00:00Z`);
  const start = time(points[0].day), end = Math.max(time(points[points.length - 1].day), ...series.checks.map(time));
  const span = Math.max(end - start, 30 * 864e5), from = start - span * 0.02, to = start + span * 1.04;
  const W = 760, H = 220, L = 96, R = 8, T = 12, B = 30;
  const x = (day) => L + (time(day) - from) / (to - from) * (W - L - R);
  const lanes = d.type === "boolean" ? [true, false] : d.type === "category" ? d.categories : null;
  let y, labels;
  if (lanes) {
    y = (v) => T + (lanes.indexOf(v) + 0.5) / lanes.length * (H - T - B);
    labels = lanes.map((v) => [y(v), v === true ? "Yes" : v === false ? "No" : v]);
  } else {
    const known = points.map((p) => p.value).filter((v) => v !== null);
    let [low, high] = known.length ? [Math.min(...known), Math.max(...known)] : [d.min ?? 0, d.max ?? 1];
    const pad = (high - low) * 0.15 || Math.abs(high) * 0.1 || 1;
    [low, high] = [Math.max(low - pad, d.min ?? -Infinity), Math.min(high + pad, d.max ?? Infinity)];
    y = (v) => T + (high - v) / (high - low) * (H - T - B);
    labels = [[y(high), inUnits(high, d.units)], [y(low), inUnits(low, d.units)]];
  }
  const ns = "http://www.w3.org/2000/svg";
  const svg = (tag, attrs, text) => {
    const node = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "series", role: "img",
                             "aria-label": `${d.key} of ${series.subject} from ${points[0].day}` });
  const written = d.written_at.slice(0, 10);
  if (time(written) >= from) chart.append(svg("rect", { x: L, y: T, width: Math.min(x(written), W - R) - L, height: H - T - B, class: "sample" }));
  for (const [at, text] of labels) {
    chart.append(svg("line", { x1: L, x2: W - R, y1: at, y2: at, class: "grid" }), svg("text", { x: L - 10, y: at + 4, "text-anchor": "end" }, text));
  }
  const last = new Date(end).toISOString().slice(0, 10);
  points.forEach((p, i) => {
    if (p.value === null) return;
    const until = i + 1 < points.length ? points[i + 1].day : last;
    chart.append(svg("line", { x1: x(p.day), x2: x(until), y1: y(p.value), y2: y(p.value), class: "step" }));
    const next = points[i + 1];
    if (next && next.value !== null) chart.append(svg("line", { x1: x(next.day), x2: x(next.day), y1: y(p.value), y2: y(next.value), class: "step" }));
    chart.append(svg("circle", { cx: x(p.day), cy: y(p.value), r: 3.5 }));
  });
  for (const p of points) if (p.value === null) chart.append(svg("text", { x: x(p.day), y: H - B - 6, class: "null" }, "null"));
  for (const day of series.checks) chart.append(svg("line", { x1: x(day), x2: x(day), y1: H - B, y2: H - B + 5, class: "check" }));
  chart.append(svg("line", { x1: L, x2: W - R, y1: H - B, y2: H - B, class: "axis" }),
               svg("text", { x: x(points[0].day), y: H - 6 }, points[0].day),
               svg("text", { x: W - R, y: H - 6, "text-anchor": "end" }, last));
  return chart;
}

function renderSeries(series) {
  const d = series.definition, written = d.written_at.slice(0, 10);
  const back = el("button", { type: "button", class: "link", text: d.key });
  back.onclick = () => openFeature(d.id);
  const node = el("div", { class: "features" }, [back, el("h1", { text: series.subject }),
    el("p", { class: "muted", text: [d.key, TYPES[d.type], d.units, `${plural(series.points.length, "value")}`,
                                     `${plural(series.checks.length, "check")}`].filter(Boolean).join(" · ") })]);
  const chart = seriesChart(series);
  if (chart) node.append(el("figure", { class: "series" }, [chart, el("figcaption", { text:
    `Shaded: dated on or before ${written}, the day the instructions were written. Ticks: dates a check found the value unchanged.` })]));
  else if (series.points.length) node.append(el("p", { class: "muted", text: "Values over several periods are listed below, not drawn." }));
  node.append(el("h2", { text: "Changes, latest first" }));
  node.append(el("div", { class: "changes" }, [...series.points].reverse().map((p) => {
    const body = el("div", { class: "record evidence" });
    const details = el("details", {}, [el("summary", {}, [el("span", { class: "date", text: p.as_of }), shown(p.value, d.units),
      el("span", { class: "muted", text: p.author_id }), ...(p.in_sample ? [el("span", { class: "tag", text: "in-sample" })] : [])]), body]);
    details.ontoggle = () => { if (details.open && !body.hasChildNodes()) openRecord(body, p.id, [], p.as_of); };
    return details;
  })));
  return node;
}

async function openSeries(feature, subject) {
  try { state.series = await api(`/api/features/${encodeURIComponent(feature)}/subjects/${encodeURIComponent(subject)}`); }
  catch (error) { return alert(error.message); }
  state.view = "series";
  render();
  $("main").scrollTop = 0;
}

async function openFeatures() {
  try { state.coverage = await api("/api/features"); } catch (error) { return alert(error.message); }
  Object.assign(state, { view: "features", current: null, thread: null });
  render();
  $("main").scrollTop = 0;
}

async function openFeature(feature) {
  try { state.feature = await api(`/api/features/${encodeURIComponent(feature)}`); } catch (error) { return alert(error.message); }
  Object.assign(state, { view: "feature", sort: { key: "subject", desc: false }, onlyNull: false });
  render();
  $("main").scrollTop = 0;
}

async function refresh() {
  if (!state.status) { try { state.status = await api("/api/status"); } catch (error) { state.status = null; } }
  state.threads = await api("/api/threads");
  if (state.current) state.thread = await api(`/api/threads/${state.current}`);
  render();
  if (state.thread && state.thread.turns.some((turn) => turn.status === "running")) setTimeout(refresh, 1500);
}

async function select(threadId) {
  state.current = threadId;
  state.view = "threads";
  await refresh();
  $("main").scrollTop = $("main").scrollHeight;
}

$("ask").onsubmit = async (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question) return;
  const run = runningRun();
  if (run) {  // A note to the run, not a new question.
    try { await api(`/api/turns/${run.id}/notes`, { text: question }); $("question").value = ""; await refresh(); } catch (e) { alert(e.message); }
    return;
  }
  try {
    const started = await api(state.current ? `/api/threads/${state.current}/turns` : "/api/threads",
      { question, effort: Number($("effort").value), agents: Number($("agents").value) });
    $("question").value = "";
    await select(started.thread_id);
  } catch (error) {
    alert(error.message);
  }
};
// What each effort level does, in the user's terms; the server holds what the agent is told.
const HINTS = {
  1: "Answers from what your graph already holds.",
  5: "Looks through your graph, adds what it learns to your layer, and answers.",
  20: "Looks through your graph and outside sources, adds what it learns to your layer, and answers.",
};
$("effort").onchange = $("agents").onchange = () => {
  const span = RUNS[$("effort").value], agents = $("agents").value;
  $("agents").hidden = !span;
  $("hint").textContent = span
    ? `Plans the work, runs ${agents === "1" ? "it with one agent" : `${agents} agents at a time`}, and reports within ${span}.`
    : HINTS[$("effort").value];
};
$("effort").onchange();
$("question").onkeydown = (event) => {  // Enter asks; Shift+Enter starts a new line.
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("ask").requestSubmit(); }
};
$("new").onclick = () => { Object.assign(state, { view: "threads", current: null, thread: null }); render(); $("question").focus(); };
$("features-view").onclick = openFeatures;
$("toggle").onclick = () => $("sidebar").classList.toggle("collapsed");
refresh();
