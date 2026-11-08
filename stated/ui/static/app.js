// The Features view: every feature definition, one feature's subjects and their latest values, and one subject's
// values over time, each opening to the evidence it rests on. Read-only.
const state = { status: null, view: "features", coverage: [], feature: null, series: null,
                sort: { key: "subject", desc: false }, onlyNull: false };
const $ = (id) => document.getElementById(id);
const api = async (path) => {
  const response = await fetch(path);
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

const number = (value) => Number.isInteger(value) ? value.toLocaleString("en-US")
  : value.toLocaleString("en-US", { maximumFractionDigits: Math.abs(value) < 10 ? 2 : 1 });

// A value in the Variable's units: fractions read as percentages, everything else as a plain number.
const inUnits = (value, units) => (units || "").startsWith("fraction") ? `${number(value * 100)}%` : number(value);


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


// Features, read-only: every definition in the workspace, then one feature with its subjects' latest values.
const TYPES = { number: "Number", count: "Count", boolean: "Yes or no", category: "Category", date: "Date" };
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
    text: "No features yet. Define one with stated define-feature." })), node;
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
  window.scrollTo(0, 0);
}

async function openFeatures() {
  try { state.coverage = await api("/api/features"); } catch (error) { return alert(error.message); }
  state.view = "features";
  render();
  window.scrollTo(0, 0);
}

async function openFeature(feature) {
  try { state.feature = await api(`/api/features/${encodeURIComponent(feature)}`); } catch (error) { return alert(error.message); }
  Object.assign(state, { view: "feature", sort: { key: "subject", desc: false }, onlyNull: false });
  render();
  window.scrollTo(0, 0);
}


function render() {
  $("features").replaceChildren(state.view === "feature" ? renderFeature(state.feature)
    : state.view === "series" ? renderSeries(state.series) : renderFeatures(state.coverage));
  renderStatus();
}

// The foot of the page: the workspace folder and the layers attached to it.
function renderStatus() {
  const status = state.status;
  if (!status) return $("status").textContent = "";
  const home = status.workspace.replace(/^\/Users\/[^/]+|^\/home\/[^/]+/, "~");
  const base = (status.layers || []).filter((layer) => !layer.writable).map((layer) => `${layer.name} ${layer.version}`);
  $("status").textContent = [home, ...(base.length ? base : ["no base layer attached"])].join(" · ");
}

(async () => {
  try { state.status = await api("/api/status"); } catch (error) { state.status = null; }
  await openFeatures();
})();
