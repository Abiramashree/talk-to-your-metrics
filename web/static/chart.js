// Charts for one metric: a line over time, bars by one category, or a single figure.
// Plain SVG; every label goes through textContent. Specs follow the dataviz guide:
// 2px lines, <=24px bars with 4px rounded ends, hairline grid, direct labels only at ends/tips.

const NS = "http://www.w3.org/2000/svg";
const MAX_BARS = 15;
// Narrow screens draw on a narrower canvas so axis text stays legible when scaled to fit.
const narrow = () => window.matchMedia("(max-width: 600px)").matches;

function svg(tag, attrs = {}, parent) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.append(node);
  return node;
}

function div(cls, text, parent) {
  const node = document.createElement("div");
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  if (parent) parent.append(node);
  return node;
}

// Round tick step (1, 2 or 5 × 10^n) so gridlines land on clean values; 3–5 intervals.
function ticks(value) {
  if (value <= 0) return { max: 1, step: 0.25 };
  const raw = value / 4, exp = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map((m) => m * exp).find((s) => s >= raw);
  return { max: Math.ceil(value / step) * step, step };
}

function tooltip(wrap) {
  const tip = div("viz-tip", undefined, wrap);
  tip.hidden = true;
  return {
    show(x, y, value, label) {
      tip.replaceChildren(div("viz-tip-value", value), div("viz-tip-label", label));
      tip.hidden = false;
      const w = wrap.clientWidth;
      tip.style.left = `${Math.min(Math.max(x - tip.offsetWidth / 2, 0), w - tip.offsetWidth)}px`;
      tip.style.top = `${Math.max(y - tip.offsetHeight - 10, 0)}px`;
    },
    hide() { tip.hidden = true; },
  };
}

function figure(value, label) {
  const wrap = div("viz-figure");
  div("viz-figure-value", value, wrap);
  div("viz-figure-label", label, wrap);
  return wrap;
}

function line(points, f) {
  const W = narrow() ? 360 : 640, H = narrow() ? 200 : 220, L = 58, R = 20, T = 16, B = 28;
  const wrap = div("viz");
  const { max, step } = ticks(Math.max(...points.map((p) => p.value)));
  const x = (i) => L + (points.length === 1 ? 0 : (i * (W - L - R)) / (points.length - 1));
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": f.title }, wrap);

  for (let v = 0; v <= max + step / 2; v += step) {
    svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "viz-grid" }, root);
    svg("text", { x: L - 8, y: y(v) + 4, class: "viz-axis", "text-anchor": "end" }, root).textContent = f.axis(v);
  }
  // every point when there are few; otherwise an even step, always including the last
  const every = points.length <= (narrow() ? 4 : 12) ? 1 : Math.ceil(points.length / (narrow() ? 4 : 6));
  for (let i = 0; i < points.length; i++) {
    if (i % every && i !== points.length - 1) continue;
    if (i !== points.length - 1 && points.length - 1 - i < every / 2) continue;  // don't crowd the last label
    const anchor = i === 0 ? "start" : i === points.length - 1 ? "end" : "middle";
    svg("text", { x: x(i), y: H - 8, class: "viz-axis", "text-anchor": anchor }, root).textContent = points[i].label;
  }
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join("");
  svg("path", { d: `${d}L${x(points.length - 1)},${y(0)}L${x(0)},${y(0)}Z`, class: "viz-area" }, root);
  svg("path", { d, class: "viz-line" }, root);
  const last = points.length - 1;
  svg("circle", { cx: x(last), cy: y(points[last].value), r: 4, class: "viz-dot" }, root);
  svg("text", { x: x(last), y: y(points[last].value) - 10, class: "viz-label", "text-anchor": "end" }, root)
    .textContent = f.value(points[last].value);

  const cross = svg("line", { y1: T, y2: H - B, class: "viz-cross", visibility: "hidden" }, root);
  const focus = svg("circle", { r: 4, class: "viz-dot", visibility: "hidden" }, root);
  const tip = tooltip(wrap);
  const show = (i) => {
    const px = x(i), py = y(points[i].value);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
    focus.setAttribute("cx", px); focus.setAttribute("cy", py); focus.setAttribute("visibility", "visible");
    const scale = wrap.clientWidth / W;
    tip.show(px * scale, py * scale, f.value(points[i].value), points[i].label);
  };
  const hide = () => { cross.setAttribute("visibility", "hidden"); focus.setAttribute("visibility", "hidden"); tip.hide(); };
  let active = last;
  root.addEventListener("pointermove", (e) => {
    const box = root.getBoundingClientRect();
    const sx = ((e.clientX - box.left) / box.width) * W;
    active = Math.max(0, Math.min(last, Math.round(((sx - L) / (W - L - R)) * last)));
    show(active);
  });
  root.addEventListener("pointerleave", hide);
  wrap.tabIndex = 0;
  wrap.addEventListener("focus", () => show(active));
  wrap.addEventListener("blur", hide);
  wrap.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight") { active = Math.min(last, active + 1); show(active); e.preventDefault(); }
    if (e.key === "ArrowLeft") { active = Math.max(0, active - 1); show(active); e.preventDefault(); }
  });
  return wrap;
}

function bars(items, f) {
  const W = narrow() ? 360 : 640, LABEL = narrow() ? 100 : 150, ROW = 28, BAR = 16;
  // room for the longest value label (11.5px tabular digits ≈ 7 units a character), so none is cut off
  const VALUE = Math.max(70, Math.ceil(Math.max(...items.map((p) => f.value(p.value).length)) * 7) + 10);
  const H = items.length * ROW + 4;
  const wrap = div("viz");
  const max = Math.max(...items.map((p) => p.value), 0) || 1;
  const width = (v) => Math.max(((W - LABEL - VALUE) * v) / max, v > 0 ? 2 : 0);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": f.title }, wrap);
  const tip = tooltip(wrap);
  items.forEach((p, i) => {
    const top = i * ROW + 4, w = width(p.value), r = Math.min(4, w / 2);
    const g = svg("g", { class: "viz-bar", tabindex: 0 }, root);
    svg("rect", { x: 0, y: top - 4, width: W, height: ROW, class: "viz-hit" }, g);
    const name = svg("text", { x: LABEL - 10, y: top + BAR - 3, class: "viz-axis", "text-anchor": "end" }, g);
    const max = narrow() ? 14 : 22;
    name.textContent = p.label.length > max ? `${p.label.slice(0, max - 1)}…` : p.label;
    // square at the baseline, 4px rounded data-end
    svg("path", { d: `M${LABEL},${top}h${w - r}a${r},${r} 0 0 1 ${r},${r}v${BAR - 2 * r}a${r},${r} 0 0 1 -${r},${r}h-${w - r}Z`, class: "viz-fill" }, g);
    svg("text", { x: LABEL + w + 6, y: top + BAR - 3, class: "viz-label" }, g).textContent = f.value(p.value);
    const show = () => {
      const scale = wrap.clientWidth / W;
      tip.show((LABEL + w) * scale, top * scale, f.value(p.value), p.label);
    };
    g.addEventListener("pointerenter", show);
    g.addEventListener("focus", show);
    g.addEventListener("pointerleave", () => tip.hide());
    g.addEventListener("blur", () => tip.hide());
  });
  return wrap;
}

// f: { isTime(col), metricLabel(col), keyLabel(col, value), value(col)(v), axis(col)(v) }
export function chart(columns, rows, f) {
  if (!rows || !rows.length) return null;
  const numeric = columns.map((_, i) => rows.every((r) => typeof r[i] === "number" || r[i] === null));
  const metrics = columns.filter((_, i) => numeric[i]);
  const keys = columns.filter((_, i) => !numeric[i]);
  if (metrics.length !== 1) return null;
  const m = columns.indexOf(metrics[0]);
  const fmt = { value: f.value(metrics[0]), axis: f.axis(metrics[0]), title: f.metricLabel(metrics[0]) };

  if (keys.length === 0 && rows.length === 1) return figure(fmt.value(rows[0][m]), fmt.title);
  if (keys.length !== 1 || rows.length < 2) return null;
  const k = columns.indexOf(keys[0]);
  const kept = rows.filter((r) => r[m] !== null);
  if (f.isTime(keys[0])) {
    // A line over time always runs in date order, whatever the table is sorted by.
    const ordered = [...kept].sort((a, b) => String(a[k]).localeCompare(String(b[k])));
    return line(ordered.map((r) => ({ label: f.keyLabel(keys[0], r[k]), value: r[m] })), fmt);
  }
  const items = kept.map((r) => ({ label: f.keyLabel(keys[0], r[k]), value: r[m] }));
  if (!items.every((p) => p.value >= 0)) return null;
  if (items.length <= MAX_BARS) return bars(items, fmt);
  const wrap = div("viz-group");
  wrap.append(bars(items.slice(0, MAX_BARS), fmt));
  div("viz-note", `First ${MAX_BARS} of ${items.length} shown; the table has all of them.`, wrap);
  return wrap;
}
