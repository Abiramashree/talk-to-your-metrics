import { chart } from "./chart.js";

// ---------- formatting (display only; the agent never sees this) ----------
const MONEY = new Set(["revenue", "freight", "gross_merchandise_value", "payments", "average_order_value"]);
const RATE = new Set(["cancellation_rate", "repeat_customer_rate", "late_delivery_rate"]);
const DECIMAL = new Set(["average_delivery_days", "average_review_score"]);
const GRAINS = ["day", "week", "month", "quarter", "year"];
const OPERATORS = ["=", "!=", ">", ">=", "<", "<=", "in", "not in"];
const PERIOD = { min: "2016-09-01", max: "2018-10-17" };

const metricName = (col) => col.split("__").pop();
const isTime = (col) => col.startsWith("metric_time");
const grainOf = (col) => (col.includes("__") ? col.split("__").pop() : "day");

function compact(v) {
  const a = Math.abs(v);
  if (a >= 1e6) return `${(v / 1e6).toFixed(a >= 1e7 ? 1 : 2)}M`;
  if (a >= 1e4) return `${(v / 1e3).toFixed(0)}K`;
  return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function valueFormatter(col) {
  const m = metricName(col);
  return (v) => {
    if (v === null || v === undefined) return "—";
    if (MONEY.has(m)) return `R$ ${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    if (RATE.has(m)) return `${(v * 100).toFixed(2)}%`;
    if (DECIMAL.has(m)) return v.toFixed(2);
    return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 4 });
  };
}

function axisFormatter(col) {
  const m = metricName(col);
  return (v) => (MONEY.has(m) ? `R$ ${compact(v)}` : RATE.has(m) ? `${(v * 100).toFixed(1)}%` : compact(v));
}

function keyLabel(col, value) {
  if (value === null) return /category/.test(col) ? "(no category)" : "(none)";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (!isTime(col) || typeof value !== "string") return String(value);
  const [y, mo] = value.split("-");
  const grain = grainOf(col);
  if (grain === "year") return y;
  if (grain === "quarter") return `${y} Q${Math.floor((Number(mo) - 1) / 3) + 1}`;
  if (grain === "month") return `${y}-${mo}`;
  return value.slice(0, 10);
}

// ---------- state ----------
const state = { metrics: [], labels: {}, dims: new Map(), cards: [], selected: null, seq: 0 };

// One follow-up offered after a suggested question, so the first visit shows a conversation.
const FOLLOW_UPS = { "Top 5 product categories by revenue": "Which of these had the most canceled orders?" };

// The conversation survives a reload in this tab (sessionStorage: this tab only, gone when it closes).
const SAVED = "ttym.chat";
const KEPT = ["question", "steps", "cannot", "failed", "query", "result", "original", "originalResult", "edited", "next"];

function save() {
  const done = state.cards.filter((c) => !c.thinking).slice(-20)
    .map((c) => Object.fromEntries(KEPT.filter((k) => c[k] !== undefined).map((k) => [k, c[k]])));
  for (let keep = done.length; keep >= 0; keep -= 5) {
    try { sessionStorage.setItem(SAVED, JSON.stringify(done.slice(done.length - keep))); return; }
    catch { /* over the storage limit: keep fewer */ }
  }
}

function restore() {
  let saved = [];
  try { saved = JSON.parse(sessionStorage.getItem(SAVED) || "[]"); } catch { return; }
  if (!Array.isArray(saved)) return;
  for (const fields of saved) {
    if (fields && typeof fields === "object") renderCard(newCard(Object.fromEntries(KEPT.map((k) => [k, fields[k]]).filter(([, v]) => v !== undefined))));
  }
}
const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;  // never innerHTML: everything shown is untrusted text
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== false && v !== null && v !== undefined) node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.filter(Boolean));
  return node;
}

const label = (col) => {
  if (isTime(col)) return grainOf(col);
  return state.labels[col] || col.replace(/^\w+__/, "").replace(/_/g, " ");
};

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({ error: "Unexpected response." }));
  if (!response.ok) throw new Error(data.error || (data.detail && "That request was not valid.") || "Something went wrong.");
  return data;
}

async function dimensions(metrics) {
  const key = [...metrics].sort().join(",");
  if (!state.dims.has(key)) {
    const qs = metrics.map((m) => `metrics=${encodeURIComponent(m)}`).join("&");
    state.dims.set(key, api(`/dimensions?${qs}`).catch(() => ({ dimensions: [] })));
  }
  return state.dims.get(key);
}

// ---------- cards ----------
function newCard(fields) {
  const card = { id: ++state.seq, edited: false, loading: false, ...fields };
  card.el = el("article", { class: "turn" });
  state.cards.push(card);
  $("empty").hidden = true;
  $("thread").append(card.el);
  return card;
}

function renderTable(columns, rows) {
  const PREVIEW = 8;
  const wrap = el("div", { class: "table-wrap" });
  const table = el("table");
  const numeric = columns.map((_, i) => rows.every((r) => typeof r[i] === "number" || r[i] === null));
  table.append(el("tr", {}, ...columns.map((c, i) => el("th", { class: numeric[i] ? "num" : "", text: label(c) }))));
  const body = rows.map((r) => el("tr", {}, ...r.map((v, i) => el("td", {
    class: numeric[i] ? "num" : "", text: numeric[i] ? valueFormatter(columns[i])(v) : keyLabel(columns[i], v),
  }))));
  table.append(...body.slice(0, PREVIEW));
  wrap.append(table);
  if (rows.length > PREVIEW) {
    const more = el("button", { class: "link", type: "button", text: `Show all ${rows.length} rows`, onclick: () => {
      table.append(...body.slice(PREVIEW)); more.remove();
    } });
    wrap.append(more);
  }
  return wrap;
}

function renderResult(card) {
  const r = card.result;
  const box = el("div", { class: `result${card.loading ? " loading" : ""}` });
  if (card.error) { box.append(el("p", { class: "error", text: card.error })); return box; }
  if (!r) return box;
  if (!r.rows.length || (r.rows.length === 1 && r.rows[0].every((v) => v === null))) {
    box.append(el("p", { class: "muted", text: "No data matches this query." }));
    return box;
  }
  const viz = chart(r.columns, r.rows, {
    isTime, metricLabel: label, keyLabel, value: valueFormatter, axis: axisFormatter,
  });
  if (viz) box.append(viz);
  const single = r.rows.length === 1 && r.columns.length === 1;
  if (!single) box.append(renderTable(r.columns, r.rows));
  if (r.truncated) box.append(el("p", { class: "muted small", text: "Showing the first 1,000 rows." }));
  return box;
}

function renderCard(card) {
  const parts = [];
  if (card.question) parts.push(el("div", { class: "you", text: card.question }));
  const reply = el("div", { class: `reply${state.selected === card ? " selected" : ""}` });
  if (card.thinking) {
    reply.append(el("div", { class: "thinking", "aria-label": "Thinking" }, el("span"), el("span"), el("span")));
  } else if (card.failed) {
    reply.append(el("p", { class: "error", text: card.failed }));
  } else if (card.cannot) {
    // A reason is shown only when it carries no numbers: those would be unchecked model text.
    reply.append(el("p", { class: "muted", text: /\d/.test(card.cannot) ? "This can't be answered from the metrics in this data." : card.cannot }));
  } else {
    // The title is built from the query itself; the model's own sentence is never shown,
    // so no number on the page comes from anywhere but the query result.
    reply.append(el("p", { class: "title", text: summary(card.query) }));
    if (card.edited) reply.append(el("p", { class: "caption", text: "Edited" }));
    reply.append(renderResult(card));
    const actions = el("div", { class: "actions" });
    actions.append(el("button", { class: "link", type: "button", text: "Edit query", onclick: (e) => { e.stopPropagation(); select(card, true); } }));
    if (card.edited && card.original) {
      actions.append(el("button", { class: "link", type: "button", text: "Reset", onclick: (e) => {
        e.stopPropagation(); card.query = structuredClone(card.original); card.result = card.originalResult;
        card.edited = false; card.error = null; renderCard(card); renderPanel();
      } }));
    }
    reply.append(actions);
    reply.addEventListener("click", () => select(card));
  }
  parts.push(reply);
  if (card.next && card === state.cards[state.cards.length - 1] && !card.thinking) {
    parts.push(el("div", { class: "suggestions follow" },
      el("button", { type: "button", text: card.next, onclick: () => { const text = card.next; card.next = null; renderCard(card); ask(text); } })));
  }
  card.el.replaceChildren(...parts);
  if (!card.thinking && !card.loading) save();
}

function summary(q) {
  if (!q) return "";
  const bits = [q.metrics.map(label).join(", ")];
  if (q.group_by.length) bits.push(`by ${q.group_by.map(label).join(", ")}`);
  if (q.start || q.end) bits.push(period(q.start, q.end));
  for (const f of q.filters) bits.push(`${label(f.dimension)} ${f.operator} ${Array.isArray(f.value) ? f.value.join(", ") : f.value}`);
  return bits.join(" · ");
}

function period(start, end) {
  const s = start || PERIOD.min, e = end || PERIOD.max;
  if (s.slice(0, 4) === e.slice(0, 4) && s.endsWith("-01-01") && e.endsWith("-12-31")) return s.slice(0, 4);
  return `${s} to ${e}`;
}

function select(card, open = false) {
  if (!card.query) return;
  const previous = state.selected;
  state.selected = card;
  if (previous && previous !== card) renderCard(previous);
  renderCard(card);
  renderPanel();
  if (open) document.body.classList.add("panel-open");
}

function scrollThread() {
  const t = $("thread");
  t.scrollTo({ top: t.scrollHeight, behavior: "smooth" });
}

function queryBody(q) {
  return {
    metrics: q.metrics, group_by: q.group_by, filters: q.filters, start: q.start || null, end: q.end || null,
    order_by: q.order_by, limit: q.limit || null,
  };
}

// The last question (or panel query) and its query as shown, so follow-ups like "which of these" work.
function previousTurn() {
  const last = [...state.cards].reverse().find((c) => c.question || c.query);
  if (!last) return null;
  return { question: (last.question || "a query built in the panel").slice(0, 300), query: last.query ? queryBody(last.query) : null };
}

// ---------- asking ----------
async function ask(question) {
  const previous = previousTurn();
  for (const c of state.cards) if (c.next) { c.next = null; renderCard(c); }  // an offered follow-up goes once you ask anything
  const card = newCard({ question, thinking: true });
  renderCard(card);
  scrollThread();
  $("question").value = "";
  $("question").disabled = true;
  try {
    const data = await api("/ask", previous ? { question, previous } : { question });
    card.thinking = false;
    const w = data.working;
    card.steps = w ? w.steps : [];
    if (data.final.cannot_answer) {
      card.cannot = data.final.cannot_answer;
    } else if (w && w.query) {
      card.query = w.query;
      // the table and chart come from the query result itself, never the model's copy
      card.result = { columns: w.columns, rows: w.rows, truncated: w.truncated, sql: w.sql, definitions: w.definitions };
      card.original = structuredClone(w.query);
      card.originalResult = card.result;
      card.next = FOLLOW_UPS[question] || null;
    } else {
      card.cannot = "No query was run for this question.";
    }
  } catch (err) {
    card.thinking = false;
    card.failed = err.message;
  }
  renderCard(card);
  scrollThread();
  if (card.query) select(card);
  $("question").disabled = false;
  $("question").focus();
}

// ---------- playground ----------
let timer = null;
function changed(card) {
  card.edited = Boolean(card.original);
  renderPanel();
  clearTimeout(timer);
  timer = setTimeout(() => run(card), 250);
}

async function run(card) {
  const ticket = (card.ticket = (card.ticket || 0) + 1);
  card.loading = true;
  renderCard(card);
  let result = null, error = null;
  try {
    result = await api("/query", queryBody(card.query));
  } catch (err) {
    error = err.message;
  }
  if (ticket !== card.ticket) return;  // a newer edit is in flight; its response wins
  if (result) { card.result = result; card.error = null; } else { card.error = error; }
  card.loading = false;
  renderCard(card);
  renderPanel();
}

function explore(metric) {
  const card = newCard({ query: { metrics: [metric], group_by: [], filters: [], start: null, end: null, order_by: [], limit: null } });
  select(card);
  document.body.classList.remove("panel-open");  // on phones, show the result; the sheet reopens via Edit query
  run(card);
}

function section(title, ...body) {
  return el("section", { class: "field" }, el("h3", { text: title }), ...body);
}

function chip(text, onremove) {
  return el("span", { class: "chip" }, el("span", { text }),
    onremove ? el("button", { type: "button", "aria-label": `Remove ${text}`, text: "×", onclick: onremove }) : null);
}

function picker(placeholder, options, onpick) {
  const s = el("select", { class: "add", "aria-label": placeholder, onchange: () => { if (s.value) onpick(s.value); } },
    el("option", { value: "", text: placeholder }));
  for (const [group, items] of options) {
    const parent = group ? el("optgroup", { label: group }) : s;
    for (const [value, text] of items) parent.append(el("option", { value, text }));
    if (group && items.length) s.append(parent);
  }
  return s;
}

async function renderPanel() {
  const body = $("panel-body");
  const card = state.selected;
  if (!card || !card.query) {
    body.replaceChildren(el("ul", { class: "catalog" }, ...state.metrics.map((m) => el("li", {},
      el("button", { type: "button", onclick: () => explore(m.name) },
        el("span", { class: "catalog-name", text: m.label || m.name }),
        m.description ? el("span", { class: "catalog-desc", text: m.description }) : null)))));
    return;
  }
  const q = card.query;
  const dims = (await dimensions(q.metrics)).dimensions || [];
  if (state.selected !== card) return;  // selection changed while loading

  const metricChips = q.metrics.map((m) => chip(label(m), q.metrics.length > 1 ? async () => {
    q.metrics = q.metrics.filter((x) => x !== m);
    await prune(q);
    changed(card);
  } : null));
  const addMetric = picker("Add metric", [[null, state.metrics.filter((m) => !q.metrics.includes(m.name)).map((m) => [m.name, m.label || m.name])]],
    async (name) => { q.metrics.push(name); await prune(q); changed(card); });

  const groupChips = q.group_by.map((g) => chip(label(g), () => {
    q.group_by = q.group_by.filter((x) => x !== g);
    q.order_by = q.order_by.filter((o) => o.replace(/^-/, "") !== g);
    changed(card);
  }));
  const addGroup = picker("Add grouping", [
    ["Time", GRAINS.map((g) => [`metric_time__${g}`, g])],
    ["Dimension", dims.filter((d) => !q.group_by.includes(d.name)).map((d) => [d.name, label(d.name)])],
  ], (name) => {
    if (isTime(name)) q.group_by = q.group_by.filter((x) => !isTime(x));
    q.group_by.push(name);
    if (!q.order_by.length && isTime(name)) q.order_by = [name];
    changed(card);
  });

  const filterChips = q.filters.map((f, i) => chip(`${label(f.dimension)} ${f.operator} ${Array.isArray(f.value) ? f.value.join(", ") : f.value}`, () => {
    q.filters.splice(i, 1); changed(card);
  }));
  const filterForm = filterAdder(dims, (f) => { q.filters.push(f); changed(card); });

  const start = el("input", { type: "date", min: PERIOD.min, max: PERIOD.max, value: q.start || "", "aria-label": "From",
    onchange: () => { q.start = start.value || null; changed(card); } });
  const end = el("input", { type: "date", min: PERIOD.min, max: PERIOD.max, value: q.end || "", "aria-label": "To",
    onchange: () => { q.end = end.value || null; changed(card); } });

  const sortable = [...q.metrics, ...q.group_by];
  const current = q.order_by[0] || "";
  const sortSelect = el("select", { "aria-label": "Sort by", onchange: () => {
    q.order_by = sortSelect.value ? [(current.startsWith("-") ? "-" : "") + sortSelect.value] : []; changed(card);
  } }, el("option", { value: "", text: "None" }), ...sortable.map((s) => el("option", { value: s, text: label(s), selected: current.replace(/^-/, "") === s })));
  const direction = el("button", { type: "button", class: "toggle", disabled: !current,
    text: current.startsWith("-") ? "Descending" : "Ascending",
    onclick: () => { q.order_by = [current.startsWith("-") ? current.slice(1) : `-${current}`]; changed(card); } });
  const limit = el("input", { type: "number", min: 1, max: 1000, value: q.limit || "", placeholder: "All", "aria-label": "Limit",
    onchange: () => { const n = parseInt(limit.value, 10); q.limit = n >= 1 && n <= 1000 ? n : null; changed(card); } });

  const r = card.result || {};
  const defs = Object.entries(r.definitions || {});
  body.replaceChildren(...[
    section("Metrics", el("div", { class: "chips" }, ...metricChips), addMetric),
    section("Group by", el("div", { class: "chips" }, ...groupChips), addGroup),
    section("Filters", el("div", { class: "chips" }, ...filterChips), filterForm),
    section("Period", el("div", { class: "row" }, start, el("span", { class: "muted", text: "to" }), end)),
    section("Sort and limit", el("div", { class: "row" }, sortSelect, direction, limit)),
    card.error ? el("p", { class: "error", text: card.error }) : null,
    defs.length ? section("Definitions", el("dl", {}, ...defs.flatMap(([m, d]) => [el("dt", { text: label(m) }), el("dd", { text: d })]))) : null,
    r.sql ? section("SQL", el("pre", { text: r.sql })) : null,
    card.steps && card.steps.length && !card.edited ? section("Agent steps", el("ol", { class: "steps" },
      ...card.steps.map((s) => el("li", { class: s.ok ? "" : "failed", text: stepText(s) })))) : null,
  ].filter(Boolean));
}

async function prune(q) {
  // After metrics change, drop groupings, filters and sorts the new set of metrics can't use.
  const names = new Set(((await dimensions(q.metrics)).dimensions || []).map((x) => x.name));
  q.group_by = q.group_by.filter((g) => isTime(g) || names.has(g));
  q.filters = q.filters.filter((f) => names.has(f.dimension));
  q.order_by = q.order_by.filter((o) => { const n = o.replace(/^-/, ""); return q.metrics.includes(n) || q.group_by.includes(n); });
}

function filterAdder(dims, onadd) {
  if (!dims.length) return el("p", { class: "muted small", text: "These metrics have no dimensions to filter by." });
  const dim = el("select", { "aria-label": "Filter dimension" }, ...dims.map((d) => el("option", { value: d.name, text: label(d.name) })));
  const op = el("select", { "aria-label": "Operator" }, ...OPERATORS.map((o) => el("option", { value: o, text: o })));
  const slot = el("span", { class: "value-slot" });
  let input;
  const refresh = () => {
    const d = dims.find((x) => x.name === dim.value);
    const values = (d && d.values ? d.values : []).filter((v) => !v.startsWith("…"));
    input = values.length && !["in", "not in"].includes(op.value)
      ? el("select", { "aria-label": "Value" }, ...values.map((v) => el("option", { value: v, text: v })))
      : el("input", { type: "text", maxlength: 200, placeholder: ["in", "not in"].includes(op.value) ? "SP, RJ" : "Value", "aria-label": "Value" });
    slot.replaceChildren(input);
  };
  dim.addEventListener("change", refresh);
  op.addEventListener("change", refresh);
  refresh();
  const add = el("button", { type: "button", class: "small-btn", text: "Add", onclick: () => {
    const raw = input.value.trim();
    if (!raw) return;
    let value = ["in", "not in"].includes(op.value) ? raw.split(",").map((s) => s.trim()).filter(Boolean) : raw;
    if (!Array.isArray(value) && /^-?\d+(\.\d+)?$/.test(value)) value = Number(value);
    if (value === "True" || value === "False") value = value === "True";
    onadd({ dimension: dim.value, operator: op.value, value });
  } });
  return el("div", { class: "filter-add" }, dim, op, slot, add);
}

function stepText(s) {
  const a = s.arguments || {};
  if (s.tool === "list_metrics") return "Listed metrics";
  if (s.tool === "get_dimensions") return `Looked up dimensions for ${(a.metrics || []).map(label).join(", ")}`;
  if (s.tool === "query_metrics") return `Queried ${summary({ metrics: a.metrics || [], group_by: a.group_by || [], filters: a.filters || [], start: a.start, end: a.end })}`;
  return s.tool;
}

// ---------- views, theme, demo ----------
function show(view) {
  $("welcome").hidden = view !== "welcome";
  $("chat-view").hidden = view !== "chat";
  $("back").hidden = view !== "chat";
  $("open-panel").hidden = view !== "chat";
  document.title = view === "chat" ? "Ask · Talk to your metrics" : "Talk to your metrics";
  if (view === "chat") $("question").focus();
}

function go(path) {
  if (location.pathname !== path) history.pushState({ app: true }, "", path);
  show(path === "/chat" ? "chat" : "welcome");
}

function currentTheme() {
  const forced = document.documentElement.dataset.theme;
  return forced || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

function labelTheme() {
  $("theme").setAttribute("aria-label", currentTheme() === "dark" ? "Switch to light mode" : "Switch to dark mode");
}

function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch { /* not saved; still applies now */ }
  labelTheme();
}

function loadDemo() {
  const figure = $("demo"), src = figure.dataset.src;
  if (!src) return;  // set once the demo GIF is recorded
  const img = $("demo-img");
  img.addEventListener("load", () => { figure.hidden = false; });
  img.src = src;
}

// ---------- start ----------
async function start() {
  show(location.pathname === "/chat" ? "chat" : "welcome");
  window.addEventListener("popstate", () => show(location.pathname === "/chat" ? "chat" : "welcome"));
  $("try").addEventListener("click", (e) => { e.preventDefault(); go("/chat"); });
  $("home").addEventListener("click", (e) => { e.preventDefault(); go("/"); });
  $("back").addEventListener("click", () => {
    if (history.state && history.state.app) history.back(); else go("/");
  });
  $("theme").addEventListener("click", toggleTheme);
  $("open-panel").addEventListener("click", () => document.body.classList.add("panel-open"));
  labelTheme();
  loadDemo();

  try {
    const data = await api("/metrics");
    state.metrics = data.metrics;
    for (const m of data.metrics) state.labels[m.name] = m.label || m.name;
  } catch { /* the panel will show an empty picker; asking still works */ }
  restore();
  renderPanel();
  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("question").value.trim();
    if (q.length >= 3) ask(q);
  });
  document.querySelectorAll(".suggestions button").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));
  $("new-query").addEventListener("click", () => {
    const previous = state.selected; state.selected = null;
    if (previous) renderCard(previous);
    renderPanel();
    document.body.classList.add("panel-open");
  });
  $("close-panel").addEventListener("click", () => document.body.classList.remove("panel-open"));
}

start();
