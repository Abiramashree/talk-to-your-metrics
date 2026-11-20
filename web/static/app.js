const $ = (id) => document.getElementById(id);
const form = $("ask"), input = $("question");

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;  // never innerHTML: answers are untrusted text
  if (cls) node.className = cls;
  return node;
}

function format(value) {
  if (typeof value !== "number") return value === null ? "—" : String(value);
  const digits = Number.isInteger(value) ? 0 : Math.abs(value) < 1 ? 4 : 2;
  return value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

// metric_time__quarter -> quarter, order__state -> state
const label = (column) => column.replace(/^metric_time__/, "").replace(/^\w+__/, "").replace(/_/g, " ");

function period(column, value) {
  const grain = column.startsWith("metric_time__") ? column.slice(13) : null;
  if (!grain || typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}/.test(value)) return null;
  const [y, m] = value.split("-");
  if (grain === "year") return y;
  if (grain === "quarter") return `${y} Q${Math.floor((Number(m) - 1) / 3) + 1}`;
  if (grain === "month") return `${y}-${m}`;
  return value.slice(0, 10);
}

function renderTable(columns, rows) {
  const table = $("table");
  table.replaceChildren();
  if (!rows || !rows.length || (rows.length === 1 && rows[0].length === 1)) return;
  const numeric = columns.map((_, i) => rows.every((r) => typeof r[i] === "number" || r[i] === null));
  const head = el("tr");
  columns.forEach((c, i) => head.append(el("th", label(c), numeric[i] ? "num" : "")));
  table.append(head);
  rows.forEach((r) => {
    const tr = el("tr");
    r.forEach((v, i) => tr.append(el("td", period(columns[i], v) ?? format(v), numeric[i] ? "num" : "")));
    table.append(tr);
  });
}

function renderWorking(w) {
  const facts = $("facts");
  facts.replaceChildren();
  if (!w) { $("how").hidden = true; return; }
  const add = (term, value) => { facts.append(el("dt", term), el("dd", value)); };
  Object.entries(w.definitions || {}).forEach(([metric, text]) => add(metric, text));
  if (w.start || w.end) add("period", `${w.start || "start"} to ${w.end || "end"}`);
  (w.filters || []).forEach((f) => add("filter", `${label(f.dimension)} ${f.operator} ${JSON.stringify(f.value)}`));
  if ((w.group_by || []).length) add("grouped by", w.group_by.map(label).join(", "));
  $("sql").textContent = w.sql;
  $("how").hidden = false;
}

async function ask(question) {
  input.value = question;
  input.disabled = true;
  $("result").hidden = false;
  $("answer").className = "muted";
  $("answer").textContent = "Thinking…";
  $("table").replaceChildren();
  $("how").hidden = true;
  try {
    const response = await fetch("/ask", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Something went wrong.");
    const final = data.final;
    if (final.cannot_answer) {
      $("answer").textContent = final.cannot_answer;
    } else {
      $("answer").className = "";
      $("answer").textContent = final.answer || "";
      // Numbers in the table come from the query result itself, never from the model's copy of them.
      if (data.working) renderTable(data.working.columns, data.working.rows);
      renderWorking(data.working);
    }
  } catch (err) {
    $("answer").textContent = err.message;
  } finally {
    input.disabled = false;
    input.focus();
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const q = input.value.trim();
  if (q.length >= 3) ask(q);
});
document.querySelectorAll("#examples button").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));
