/* Logistics and Network Design (Portfolio Output 6) - network control tower. Renders every view in the browser from data/payload.json. */
(() => {
  "use strict";
  const C = { ink: "#1F2A44", teal: "#0E7C86", slate: "#6B7A90", good: "#3C8D5A", watch: "#E3A21A", breach: "#C2413B", fog: "#B7C0CD" };
  const STATUS_COLOR = { good: C.good, watch: C.watch, breach: C.breach, info: C.slate };
  const STATUS_TEXT = { good: "On target", watch: "Watch", breach: "Breach", info: "Defer" };
  const CITY_COLORS = ["#1F2A44", "#0E7C86", "#E3A21A", "#8E5A9B", "#C2413B"];
  const VTYPE = { VAN_2T: "2-ton van", TEMPO_1T: "1-ton tempo", TWO_WHEELER: "2-wheeler" };
  const TABS = ["overview", "routes", "fleet", "warehouse", "simulation", "network"];
  const TITLES = { overview: "Command center", routes: "Last-mile routing", fleet: "Fleet planning", warehouse: "Warehouse slotting",
    simulation: "DC operations", network: "Network & finance" };
  const SCN = { base: "Baseline staffing", opt: "Optimized staffing", dwBase: "Diwali peak - baseline staffing", dwPeak: "Diwali peak - peak roster" };
  const SCN_LABEL = { [SCN.base]: "Today's roster", [SCN.opt]: "Recommended roster", [SCN.dwBase]: "Diwali, today's roster", [SCN.dwPeak]: "Diwali, peak roster" };
  const NO_TILES = Boolean(window.__NO_TILES);
  const state = { city: null, tab: "overview", plan: "optimized", p: 1, wacc: 0.12, growth: 0.18 };
  let P = null;

  /* ---------------------------------------------------------------- helpers */
  const $ = (id) => document.getElementById(id);
  const css = (v, f) => getComputedStyle(document.documentElement).getPropertyValue(v).trim() || f;
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const pct = (v, d = 1) => (v * 100).toFixed(d) + "%";
  const int = (v) => Math.round(v).toLocaleString("en-IN");
  const inr = (v) => (v < 0 ? "−" : "") + "₹" + Math.round(Math.abs(v)).toLocaleString("en-IN");
  const inrS = (v) => {                                 // Indian short form: lakh / crore
    const a = Math.abs(v), s = v < 0 ? "−" : "";
    if (a >= 1e7) return `${s}₹${(a / 1e7).toFixed(2)} cr`;
    if (a >= 1e5) return `${s}₹${(a / 1e5).toFixed(2)} L`;
    return `${s}₹${int(a)}`;
  };
  const higher = (v, good, watch) => (v >= good ? "good" : v >= watch ? "watch" : "breach");
  const lower = (v, good, watch) => (v <= good ? "good" : v <= watch ? "watch" : "breach");
  const pill = (s, text) => `<span class="pill ${s}">${esc(text || STATUS_TEXT[s])}</span>`;
  const fmtDate = (iso) => new Date(iso + "T00:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  const cityName = (id) => P.meta.cities.find((c) => c.id === id).name;
  const cityIds = () => P.meta.cities.map((c) => c.id);
  const staffTxt = (s) => `${s[0]} / ${s[1]} / ${s[2]} / ${s[3]}`;

  function baseLayout(extra = {}) {
    const rule = css("--rule", "#DCE1E8");
    return Object.assign({
      margin: { l: 58, r: 14, t: 30, b: 40 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
      font: { family: "IBM Plex Sans, Segoe UI, Arial, sans-serif", size: 12, color: css("--text", "#1C2436") },
      xaxis: { gridcolor: rule, zeroline: false, linecolor: rule, automargin: true },
      yaxis: { gridcolor: rule, zeroline: false, automargin: true },
      legend: { orientation: "h", y: 1.02, yanchor: "bottom", x: 0, font: { size: 11.5 } },
      hoverlabel: { font: { family: "IBM Plex Sans, Arial", size: 12 }, bordercolor: rule },
      bargap: 0.28,
    }, extra);
  }
  const cfg = { displayModeBar: false, responsive: true };
  const plot = (id, data, lay) => Plotly.react($(id), data, baseLayout(lay), cfg);
  const targetLine = (axis, v, color = C.ink, dash = "dash") => (axis === "y"
    ? { type: "line", xref: "paper", x0: 0, x1: 1, y0: v, y1: v, line: { color, width: 1.5, dash } }
    : { type: "line", yref: "paper", y0: 0, y1: 1, x0: v, x1: v, line: { color, width: 1.5, dash } });

  /* maps: street basemap where tiles are reachable, coordinate axes otherwise */
  function mapTrace(lat, lon, props) {
    return NO_TILES ? Object.assign({ type: "scatter", x: lon, y: lat }, props) : Object.assign({ type: "scattermap", lat, lon }, props);
  }
  function mapLayout(center) {
    if (NO_TILES) {
      return { xaxis: { visible: false }, yaxis: { visible: false, scaleanchor: "x", scaleratio: 1 / Math.cos((center[0] * Math.PI) / 180) },
        plot_bgcolor: css("--rule-2", "#EEF1F5") };
    }
    return { map: { style: "carto-positron", center: { lat: center[0], lon: center[1] }, zoom: 9.6 } };
  }

  /* ---------------------------------------------------------------- metrics */
  function metrics(c) {
    const k = P.routing.kpis[c], b = k.baseline, o = k.optimized;
    const w = P.warehouse[c];
    const sim = Object.fromEntries(P.simulation[c].map((s) => [s.scenario, s]));
    const target = P.meta.compliance_target;
    const fl = P.fleet[c];
    const ok = fl.filter((r) => r.cur_comp > target).map((r) => r.m);
    const holds = ok.length ? Math.max(...ok) : 0;
    const rank = P.network.ranking.find((r) => r.city_id === c);
    const days = P.demand[c];
    const m = {
      b, o, w, sim, fl, holds, rank, ordersDay: days.reduce((a, x) => a + x, 0) / days.length,
      costRed: 1 - o.last_mile_cost / b.last_mile_cost, ontime: o.tw_compliance,
      travel: w.comparison.travel_reduction_pct / 100, thr: w.comparison.throughput_gain_pct / 100,
    };
    m.s = {
      cost: higher(m.costRed, 0.15, 0.10), ontime: higher(m.ontime, target, target - 0.02), fleet: higher(holds, 1.5, 1.0),
      travel: higher(m.travel, 0.35, 0.25), thr: higher(m.thr, 0.25, 0.20),
      p95: lower(sim[SCN.base].pooled_p95, P.meta.target_cycle, P.meta.target_cycle * 1.25),
      p95opt: lower(sim[SCN.opt].pooled_p95, P.meta.target_cycle, P.meta.target_cycle * 1.25),
      diwali: lower(sim[SCN.dwBase].pooled_p95, P.meta.target_cycle, P.meta.target_cycle * 1.25),
      sla: higher(rank.baseline_sla_2h, 0.90, 0.75),
      be: rank.break_even_multiplier == null ? "info" : rank.break_even_multiplier <= 1 ? "good" : rank.break_even_multiplier <= 3 ? "watch" : "info",
    };
    return m;
  }
  const moduleStatus = (c) => {
    const m = metrics(c);
    return { overview: higher(P.kpis.health.score, 80, 65), routes: m.s.ontime, fleet: m.s.fleet, warehouse: m.s.thr,
      simulation: m.s.p95, network: m.s.sla };
  };

  /* ---------------------------------------------------------------- shell */
  function buildShell() {
    $("cityStrip").innerHTML = P.meta.cities.map((c) =>
      `<button data-city="${c.id}" aria-pressed="false"><b>${esc(c.name)}</b><span>${esc(c.states.join(", "))}</span></button>`).join("");
    $("cityStrip").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-city]");
      if (b) { state.city = b.dataset.city; render(); }
    });
    $("tabs").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-tab]");
      if (b) { state.tab = b.dataset.tab; history.replaceState(null, "", "#" + state.tab); render(); window.scrollTo(0, 0); }
    });
    $("planSeg").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-v]");
      if (b) { state.plan = b.dataset.v; render(); }
    });
    $("pSlider").addEventListener("input", (e) => { state.p = +e.target.value; render(); });
    $("waccSlider").addEventListener("input", (e) => { state.wacc = +e.target.value / 100; render(); });
    $("growthSlider").addEventListener("input", (e) => { state.growth = +e.target.value / 100; render(); });
    document.addEventListener("click", (e) => {
      const r = e.target.closest("tr[data-city]");
      if (r) { state.city = r.dataset.city; render(); }
    });
    window.addEventListener("hashchange", () => { const h = location.hash.slice(1); if (TABS.includes(h)) { state.tab = h; render(); } });
    const d = P.demand.dates;
    $("contextMeta").innerHTML = `<span>Plan day <b>${fmtDate(P.meta.day)}</b></span><span>History <b>${fmtDate(d[0])} to ${fmtDate(d[d.length - 1])}</b></span>`;
  }
  function syncShell() {
    document.querySelectorAll("#cityStrip button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.city === state.city)));
    document.querySelectorAll("#tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === state.tab)));
    const st = moduleStatus(state.city);
    document.querySelectorAll("#tabs .dot").forEach((d) => { d.className = "dot " + st[d.dataset.status]; });
    TABS.forEach((t) => { $("panel-" + t).hidden = t !== state.tab; });
    document.querySelectorAll("#planSeg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === state.plan)));
    $("pOut").textContent = state.p;
    $("waccOut").textContent = pct(state.wacc, 1);
    $("growthOut").textContent = pct(state.growth, 0);
    $("pageTitle").textContent = state.tab === "overview" ? TITLES.overview : `${TITLES[state.tab]}: ${cityName(state.city)}`;
  }
  function tiles(items) {
    $("kpis").innerHTML = items.map((t) => `<div class="kpi ${t.s || ""}"><div class="l">${esc(t.l)}</div><div class="v">${t.v}</div>
      <div class="c">${t.s ? pill(t.s, t.st) : ""}<span>${t.c || ""}</span></div></div>`).join("");
  }
  const table = (head, rows) => `<table><thead><tr>${head.map((h) => `<th${h.l ? ' class="l"' : ""}>${h.t}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
  const bar = (frac, amber) => `<span class="bar${amber ? " amber" : ""}" style="width:${Math.max(2, Math.min(1, frac) * 56)}px"></span>`;

  /* ---------------------------------------------------------------- command center */
  function overview() {
    const ids = cityIds();
    const all = Object.fromEntries(ids.map((c) => [c, metrics(c)]));
    const m = all[state.city];
    const nat = P.kpis.national;
    const dw = ids.map((c) => all[c].sim[SCN.dwBase].pooled_p95);
    $("pageAnswer").innerHTML = `Optimized routing cuts network last-mile cost by <b>${pct(nat.comparison.last_mile_cost_reduction_pct / 100)}</b> with <b>${pct(nat.optimized.tw_compliance)}</b> of orders on time. The biggest open risk is Diwali: today's DC rosters dispatch the slowest 5% of orders in <b>${Math.min(...dw).toFixed(0)}–${Math.max(...dw).toFixed(0)} minutes</b> against a 120-minute promise.`;
    tiles([
      { l: "Last-mile cost reduction", v: pct(m.costRed), c: `${inrS(m.b.last_mile_cost)} → ${inrS(m.o.last_mile_cost)} a day`, s: m.s.cost },
      { l: "On time in window", v: pct(m.ontime), c: `manual dispatch ${pct(m.b.tw_compliance)}`, s: m.s.ontime },
      { l: "Vehicles on the road", v: `${m.o.vehicles_used}`, c: `vs ${m.b.vehicles_used} today, of 25`, s: "good", st: `−${m.b.vehicles_used - m.o.vehicles_used}` },
      { l: "Picker walking reduction", v: pct(m.travel), c: "target 35%", s: m.s.travel },
      { l: "DC dispatch time, P95", v: `${m.sim[SCN.base].pooled_p95.toFixed(0)} min`, c: `${m.sim[SCN.opt].pooled_p95.toFixed(0)} min with recommended roster`, s: m.s.p95 },
      { l: "Diwali dispatch time, P95", v: `${m.sim[SCN.dwBase].pooled_p95.toFixed(0)} min`, c: `today's roster; promise 120 min`, s: m.s.diwali },
    ]);
    const cell = (s, txt) => `<td class="${s}">${txt}</td>`;
    $("scorecard").innerHTML = table(
      [{ t: "City", l: 1 }, { t: "Orders/day" }, { t: "Cost cut" }, { t: "On time" }, { t: "Fleet holds to" }, { t: "Picker walking" },
       { t: "P95 today" }, { t: "P95, new roster" }, { t: "Diwali P95" }, { t: "2-hour reach" }, { t: "MFC break-even" }],
      ids.map((c) => { const x = all[c]; return `<tr class="clickable${c === state.city ? " sel" : ""}" data-city="${c}">
        <td class="l"><b>${esc(cityName(c))}</b><span class="sub">${esc(P.meta.cities.find((y) => y.id === c).states.join(", "))}</span></td>
        <td>${int(x.ordersDay)}</td>${cell(x.s.cost, "−" + pct(x.costRed))}${cell(x.s.ontime, pct(x.ontime))}
        ${cell(x.s.fleet, x.holds ? x.holds.toFixed(x.holds % 1 ? 2 : 1) + "×" : "< 0.5×")}${cell(x.s.travel, "−" + pct(x.travel))}
        ${cell(x.s.p95, x.sim[SCN.base].pooled_p95.toFixed(0) + " min")}${cell(x.s.p95opt, x.sim[SCN.opt].pooled_p95.toFixed(0) + " min")}
        ${cell(x.s.diwali, x.sim[SCN.dwBase].pooled_p95.toFixed(0) + " min")}${cell(x.s.sla, pct(x.rank.baseline_sla_2h, 0))}
        ${cell(x.s.be === "info" ? "na" : x.s.be, x.rank.break_even_multiplier ? x.rank.break_even_multiplier.toFixed(1) + "×" : "n/a")}</tr>`; }));

    const names = ids.map(cityName);
    plot("ovCost", [
      { type: "bar", name: "Manual dispatch", x: names, y: ids.map((c) => all[c].b.last_mile_cost / 1e5), marker: { color: C.fog },
        hovertemplate: "%{x}: ₹%{y:.2f} L<extra>Manual dispatch</extra>" },
      { type: "bar", name: "Optimized routes", x: names, y: ids.map((c) => all[c].o.last_mile_cost / 1e5),
        marker: { color: ids.map((c) => (c === state.city ? C.ink : C.teal)) },
        text: ids.map((c) => "−" + pct(all[c].costRed, 0)), textposition: "outside", cliponaxis: false,
        hovertemplate: "%{x}: ₹%{y:.2f} L<extra>Optimized</extra>" },
    ], { barmode: "group", yaxis: { title: { text: "₹ lakh per day" }, gridcolor: css("--rule", "#DCE1E8") }, margin: { l: 58, r: 10, t: 34, b: 40 } });

    const range = (arr, f = (v) => v.toFixed(0)) => (Math.min(...arr) === Math.max(...arr) ? f(arr[0]) : `${f(Math.min(...arr))}–${f(Math.max(...arr))}`);
    const list = (cs) => (cs.length === ids.length ? "All five cities" : cs.map(cityName).join(", "));
    const S = (c, k) => all[c].sim[k];
    const actions = [];
    actions.push({ s: "breach", area: "DC operations", cities: ids, a: "Book peak rosters for the Diwali build-up, 10–19 Oct",
      e: `P95 dispatch ${range(ids.map((c) => S(c, SCN.dwBase).pooled_p95))} → ${range(ids.map((c) => S(c, SCN.dwPeak).pooled_p95))} min; clears ${range(ids.map((c) => S(c, SCN.dwBase).backlog), int)} orders a day of backlog per DC` });
    const slow = ids.filter((c) => S(c, SCN.base).pooled_p95 > P.meta.target_cycle);
    if (slow.length) actions.push({ s: "breach", area: "DC operations", cities: slow, a: "Add one packer and one loader per shift (recommended rosters)",
      e: `P95 ${range(slow.map((c) => S(c, SCN.base).pooled_p95))} → ${range(slow.map((c) => S(c, SCN.opt).pooled_p95))} min for ${range(slow.map((c) => S(c, SCN.opt).cost - S(c, SCN.base).cost), inrS)} a day per DC` });
    const tight = ids.filter((c) => all[c].holds < 1.5);
    if (tight.length) actions.push({ s: "watch", area: "Fleet", cities: tight, a: "Arrange leased vehicles before volume reaches 1.5× the plan day",
      e: `today's 25-vehicle fleets hold the 95% standard only to ${range(tight.map((c) => all[c].holds), (v) => v + "×")}` });
    actions.push({ s: "good", area: "Routing", cities: ids, a: "Move dispatch to optimized routes, city by city",
      e: `${inrS(nat.baseline.last_mile_cost - nat.optimized.last_mile_cost)} a day saved across the network, ${nat.comparison.vehicles_saved} fewer vehicles; watch duty overruns` });
    actions.push({ s: "watch", area: "Warehouse", cities: ids, a: "Re-slot all five DCs; pilot zone picking to go past the layout ceiling",
      e: `walking −${range(ids.map((c) => all[c].travel * 100), (v) => v.toFixed(0) + "%")}, pick rate +${range(ids.map((c) => all[c].thr * 100), (v) => v.toFixed(0) + "%")}` });
    const top = P.network.ranking[0];
    actions.push({ s: "info", area: "Network", cities: [top.city_id], a: "Defer new micro-fulfilment centres; review when volume grows",
      e: `first to pay back is ${top.best_sites} at about ${int(top.break_even_parcels_per_day)} parcels a day (${top.break_even_multiplier.toFixed(1)}× today)` });
    $("actions").innerHTML = table([{ t: "Action", l: 1 }, { t: "Where", l: 1 }, { t: "Expected effect", l: 1 }],
      actions.map((x) => `<tr><td class="l wrap">${pill(x.s, x.area)} <b>${esc(x.a)}</b></td><td class="l wrap">${esc(list(x.cities))}</td><td class="l wrap">${esc(x.e)}</td></tr>`));

    plot("ovDemand", ids.map((c, i) => ({
      type: "scatter", mode: "lines", name: cityName(c), x: P.demand.dates, y: P.demand[c],
      line: { color: CITY_COLORS[i], width: c === state.city ? 2.6 : 1.2 }, opacity: c === state.city ? 1 : 0.5,
    })), {
      shapes: [{ type: "rect", xref: "x", yref: "paper", x0: "2025-10-10", x1: "2025-10-19", y0: 0, y1: 1, fillcolor: C.watch, opacity: 0.14, line: { width: 0 } },
               { type: "rect", xref: "x", yref: "paper", x0: "2025-09-23", x1: "2025-10-05", y0: 0, y1: 1, fillcolor: C.slate, opacity: 0.07, line: { width: 0 } }],
      annotations: [{ x: "2025-10-14", y: 1, yref: "paper", text: "Diwali peak", showarrow: false, yanchor: "bottom", font: { size: 11 } },
                    { x: "2025-09-29", y: 1, yref: "paper", text: "Festive sale", showarrow: false, yanchor: "bottom", font: { size: 11, color: C.slate } }],
      yaxis: { title: { text: "orders per day" }, gridcolor: css("--rule", "#DCE1E8") }, margin: { l: 58, r: 10, t: 46, b: 40 },
      legend: { orientation: "h", y: 1.1, yanchor: "bottom", x: 1, xanchor: "right", font: { size: 11.5 } },
    });

    const h = P.kpis.health, comps = Object.keys(h.components);
    const label = { delivery_compliance: "On-time delivery", last_mile_cost: "Last-mile cost", warehouse_throughput: "Pick throughput",
      dc_readiness: "DC readiness incl. Diwali", fleet_scalability: "Fleet headroom" };
    $("healthNote").textContent = `Network score ${h.score.toFixed(0)} of 100; each bar is scored 0–100 against its target`;
    plot("ovHealth", [{ type: "bar", orientation: "h", y: comps.map((k) => label[k] || k), x: comps.map((k) => h.components[k]),
      marker: { color: comps.map((k) => STATUS_COLOR[higher(h.components[k], 80, 60)]) }, text: comps.map((k) => h.components[k].toFixed(0)),
      textposition: "outside", cliponaxis: false, hovertemplate: "%{y}: %{x:.0f}<extra></extra>" }],
      { margin: { l: 170, r: 30, t: 8, b: 26 }, xaxis: { range: [0, 105], gridcolor: css("--rule", "#DCE1E8") }, yaxis: { autorange: "reversed" } });

    const sev = { high: "breach", medium: "watch", low: "info", info: "info" };
    $("alerts").innerHTML = table([{ t: "Severity", l: 1 }, { t: "Area", l: 1 }, { t: "City", l: 1 }, { t: "What is happening", l: 1 }, { t: "Orders affected" }],
      P.alerts.map((a) => `<tr><td class="l">${pill(sev[a.severity], a.severity[0].toUpperCase() + a.severity.slice(1))}</td><td class="l">${esc(a.area)}</td>
        <td class="l">${esc(cityName(a.city_id))}</td><td class="l wrap">${esc(a.message)}</td><td>${a.impact_orders ? int(a.impact_orders) : "–"}</td></tr>`));
  }

  /* ---------------------------------------------------------------- routing */
  function routes() {
    const m = metrics(state.city), plan = state.plan, k = P.routing.kpis[state.city][plan];
    const depName = Object.fromEntries(P.routing.depots[state.city].map((d) => [d[0], d[1]]));
    $("pageAnswer").innerHTML = `Optimized routes deliver the plan day's ${int(m.o.orders)} orders with <b>${m.o.vehicles_used} vehicles instead of ${m.b.vehicles_used}</b>, cutting last-mile cost by <b>${pct(m.costRed)}</b> while <b>${pct(m.o.tw_compliance)}</b> arrive inside their window (manual dispatch: ${pct(m.b.tw_compliance)}).`;
    tiles([
      { l: "Last-mile cost per day", v: inrS(k.last_mile_cost), c: plan === "optimized" ? `vs ${inrS(m.b.last_mile_cost)} manual` : "manual dispatch", s: plan === "optimized" ? m.s.cost : "" },
      { l: "On time in window", v: pct(k.tw_compliance), c: "standard 95%", s: higher(k.tw_compliance, P.meta.compliance_target, P.meta.compliance_target - 0.02) },
      { l: "Vehicles used", v: `${k.vehicles_used}`, c: "of 25 in the city" },
      { l: "Road distance", v: `${int(k.road_km)} km`, c: `estimate; ${int(k.straight_km)} km straight-line` },
      { l: "Cost per order", v: inr(k.cost_per_order), c: `load used ${k.avg_load_util_pct.toFixed(0)}%` },
      { l: "Unserved orders", v: `${k.unassigned}`, c: `${k.duration_violations} duty overruns`, s: k.unassigned === 0 ? "good" : k.unassigned <= 3 ? "watch" : "breach" },
    ]);
    const rs = P.routing.routes[state.city][plan];
    const pal = ["#0E7C86", "#E3A21A", "#1F2A44", "#8E5A9B", "#3C8D5A", "#C2413B", "#2F6DB5", "#B8860B"];
    const traces = rs.map((r, i) => mapTrace(r.pts.map((p) => p[0]), r.pts.map((p) => p[1]), {
      mode: "lines+markers", line: { width: 2.2, color: pal[i % 8] }, marker: { size: 5, color: pal[i % 8] }, name: r.v,
      hovertemplate: `${r.v}, ${VTYPE[r.type]}<br>${r.stops} stops, ${r.km} km, ${inr(r.cost)}<br>${r.late} late${r.overrun ? ", duty overrun" : ""}<extra></extra>` }));
    const dep = P.routing.depots[state.city];
    traces.push(mapTrace(dep.map((d) => d[3]), dep.map((d) => d[4]), { mode: "markers+text", marker: { size: 13, color: C.ink, symbol: "square" },
      text: dep.map((d) => d[1]), textposition: "top right", textfont: { size: 12, color: C.ink }, name: "Depots", hoverinfo: "text" }));
    const un = P.routing.unassigned[state.city][plan];
    if (un.length) traces.push(mapTrace(un.map((u) => u[0]), un.map((u) => u[1]), { mode: "markers", marker: { size: 11, color: C.breach },
      name: "Unserved", text: un.map((u) => u[2]), hoverinfo: "text" }));
    const center = P.meta.cities.find((x) => x.id === state.city).center;
    Plotly.react($("routeMap"), traces, baseLayout(Object.assign({ margin: { l: 0, r: 0, t: 0, b: 0 }, showlegend: false }, mapLayout(center))), cfg);
    $("routeNote").textContent = `${rs.length} tours from ${dep.length} depots on ${fmtDate(P.meta.day)}`;
    const z = P.routing.zones[state.city].filter((r) => r[0] === plan).sort((a, b) => a[2] - b[2]);
    const tgt = P.meta.compliance_target;
    plot("zoneBar", [{ type: "bar", orientation: "h", y: z.map((r) => r[1]), x: z.map((r) => r[2]),
      marker: { color: z.map((r) => STATUS_COLOR[higher(r[2], tgt, tgt - 0.02)]) }, text: z.map((r) => pct(r[2])), textposition: "inside",
      insidetextanchor: "end", textfont: { color: "#fff" }, hovertemplate: "%{y}: %{x:.1%}<extra></extra>" }],
      { margin: { l: 120, r: 10, t: 4, b: 28 }, xaxis: { range: [0.8, 1.0], tickformat: ".0%", gridcolor: css("--rule", "#DCE1E8") },
        shapes: [targetLine("x", tgt)] });
    $("routeStats").innerHTML = [["Orders served", `${k.served} of ${k.orders}`], ["Fleet cost", inrS(k.fleet_cost)],
      ["Outsourced orders cost", inrS(k.outsourcing_cost)], ["Duty used, average", `${k.avg_time_util_pct.toFixed(0)}%`]]
      .map(([l, v]) => `<div><dt>${l}</dt><dd>${v}</dd></div>`).join("");
    const sorted = [...rs].sort((a, b) => b.cost - a.cost);
    $("routeTable").innerHTML = table(
      [{ t: "Vehicle", l: 1 }, { t: "Type", l: 1 }, { t: "Depot", l: 1 }, { t: "Stops" }, { t: "Road km" }, { t: "Duty used", l: 1 }, { t: "Late stops" }, { t: "Tour cost" }, { t: "Status", l: 1 }],
      sorted.map((r) => `<tr><td class="l">${r.v}</td><td class="l">${VTYPE[r.type]}</td><td class="l">${esc(depName[r.depot] || r.depot)}</td>
        <td>${r.stops}</td><td>${r.km.toFixed(0)}</td><td class="l">${bar(r.util / 100, r.util > 100)}${r.util.toFixed(0)}%</td>
        <td>${r.late}</td><td>${inr(r.cost)}</td><td class="l">${r.overrun ? pill("watch", "Duty overrun") : r.late ? pill("watch", "Late stops") : pill("good", "On plan")}</td></tr>`));
  }

  /* ---------------------------------------------------------------- fleet */
  function fleet() {
    const m = metrics(state.city), f = m.fl, tgt = P.meta.compliance_target;
    const at = (x) => f.find((r) => r.m === x);
    const one = at(1) || f[0];                         // plan-day level
    const top = f[f.length - 1];                       // highest level modelled (3x in the full run)
    const mid = f.find((r) => r.m > 1 && r.m < top.m) || f.find((r) => r.m < 1) || top;
    const failDays = f.filter((r) => r.cur_comp <= tgt).reduce((a, r) => a + r.days, 0);
    $("pageAnswer").innerHTML = `The plan day needs <b>${one.rec} vehicles</b> in ${esc(cityName(state.city))}. Today's 25-vehicle fleet holds the 95% standard up to <b>${m.holds ? m.holds + "×" : "less than 0.5×"}</b> the plan-day volume; at ${top.m}× the recommended fleet is <b>${top.rec}</b> vehicles.`;
    tiles([
      { l: "Fleet needed, plan-day volume", v: `${one.rec}`, c: `${one.vans} vans, ${one.tempos} tempos, ${one.tw} 2-wheelers` },
      { l: "Today's fleet holds standard to", v: m.holds ? `${m.holds}×` : "< 0.5×", c: "of plan-day volume", s: m.s.fleet },
      { l: `Fleet needed at ${mid.m}×`, v: `${mid.rec}`, c: `on time ${pct(mid.rec_comp)}` },
      { l: `Fleet needed at ${top.m}×`, v: `${top.rec}`, c: `on time ${pct(top.rec_comp)}`, s: top.feasible ? "good" : "breach", st: top.feasible ? "Feasible" : "Infeasible" },
      { l: "Annual cost, plan-day fleet", v: inrS(Math.min(one.own, one.lease)), c: one.lease < one.own ? "leasing is cheaper" : "owning is cheaper" },
      { l: "History days above capacity", v: `${failDays}`, c: "of 90, at today's fleet", s: failDays === 0 ? "good" : failDays <= 10 ? "watch" : "breach" },
    ]);
    const lv = f.map((r) => r.m + "×");
    plot("fleetCurve", [
      { type: "bar", name: "2-ton vans", x: lv, y: f.map((r) => r.vans), marker: { color: C.ink } },
      { type: "bar", name: "1-ton tempos", x: lv, y: f.map((r) => r.tempos), marker: { color: C.teal } },
      { type: "bar", name: "2-wheelers", x: lv, y: f.map((r) => r.tw), marker: { color: C.watch } },
    ], { barmode: "stack", shapes: [targetLine("y", 25, C.breach)], xaxis: { title: { text: "demand vs plan day" } },
         yaxis: { title: { text: "vehicles" }, gridcolor: css("--rule", "#DCE1E8") },
         annotations: [{ xref: "paper", x: 0, y: 25, text: "today's fleet: 25", showarrow: false, yanchor: "bottom", xanchor: "left", font: { size: 11, color: C.breach } }] });
    plot("fleetComp", [
      { type: "scatter", mode: "lines+markers", name: "Today's fleet", x: lv, y: f.map((r) => r.cur_comp), line: { color: C.slate, width: 2.5 } },
      { type: "scatter", mode: "lines+markers", name: "Recommended fleet", x: lv, y: f.map((r) => r.rec_comp), line: { color: C.teal, width: 2.5 } },
    ], { shapes: [targetLine("y", tgt, C.watch, "dot")], yaxis: { tickformat: ".0%", range: [0.55, 1.01], gridcolor: css("--rule", "#DCE1E8") } });
    $("fleetTable").innerHTML = table(
      [{ t: "Demand", l: 1 }, { t: "Orders" }, { t: "History days" }, { t: "Today's fleet on time" }, { t: "Recommended" }, { t: "Vans / tempos / 2W" }, { t: "On time" }, { t: "Cost per order" }, { t: "Status", l: 1 }],
      f.map((r) => `<tr><td class="l">${r.m}×</td><td>${int(r.orders)}</td><td>${r.days}</td>
        <td class="${higher(r.cur_comp, tgt, tgt - 0.02)}">${pct(r.cur_comp)}</td><td><b>${r.rec}</b></td><td>${r.vans} / ${r.tempos} / ${r.tw}</td>
        <td>${pct(r.rec_comp)}</td><td>${inr(r.cpo)}</td><td class="l">${r.feasible ? pill("good", "Feasible") : pill("breach", "Infeasible")}</td></tr>`));
    plot("fleetTco", [
      { type: "bar", name: "Own", x: lv, y: f.map((r) => r.own / 1e7), marker: { color: C.ink }, hovertemplate: "%{x}: ₹%{y:.2f} cr<extra>Own</extra>" },
      { type: "bar", name: "Lease", x: lv, y: f.map((r) => r.lease / 1e7), marker: { color: C.teal }, hovertemplate: "%{x}: ₹%{y:.2f} cr<extra>Lease</extra>" },
    ], { barmode: "group", yaxis: { title: { text: "₹ crore a year" }, gridcolor: css("--rule", "#DCE1E8") } });
  }

  /* ---------------------------------------------------------------- warehouse */
  function warehouse() {
    const m = metrics(state.city), w = m.w, c = w.comparison, pr = w.perf;
    $("pageAnswer").innerHTML = `Re-slotting the ${esc(cityName(state.city))} DC cuts picker walking by <b>${pct(m.travel)}</b> and lifts pick rate by <b>${pct(m.thr)}</b>. That is within ${(c.perfect_info_travel_reduction_pct - c.travel_reduction_pct).toFixed(1)} points of what perfect demand knowledge would achieve, so the 35% walking target needs a different picking method, not a better layout.`;
    tiles([
      { l: "Picker walking reduction", v: pct(m.travel), c: "target 35%", s: m.s.travel },
      { l: "Pick rate gain", v: "+" + pct(m.thr), c: "target 25–30%", s: m.s.thr },
      { l: "Labour per 1,000 picks", v: inr(pr.optimized.labour_cost_per_1000_picks), c: `was ${inr(pr.baseline.labour_cost_per_1000_picks)}` },
      { l: "Ceiling with perfect demand knowledge", v: pct(c.perfect_info_travel_reduction_pct / 100), c: "walking reduction" },
      { l: "Heavy items above safe height", v: `${pr.optimized.heavy_slot_violations}`, c: `was ${int(pr.baseline.heavy_slot_violations)}`, s: "good", st: "Fixed" },
      { l: "Co-picked item clusters", v: int(c.clusters), c: `${int(c.clustered_skus)} items kept together` },
    ]);
    const zmax = Math.max(...w.grid.baseline.flat(), ...w.grid.optimized.flat());
    const heat = (g, show) => [{ type: "heatmap", z: g, x: g[0].map((_, i) => i + 1), y: g.map((_, i) => i + 1), zmin: 0, zmax,
      colorscale: [[0, "#F7F9FB"], [0.25, "#CFE7E9"], [0.6, "#2E9AA2"], [1, "#1F2A44"]], showscale: show,
      colorbar: { title: { text: "picks", side: "right" }, thickness: 10 }, hovertemplate: "aisle %{x}, bay %{y}: %{z:.0f} picks<extra></extra>" }];
    const hl = { margin: { l: 44, r: 10, t: 4, b: 36 }, xaxis: { title: { text: "aisle (further from dispatch →)" } }, yaxis: { title: { text: "bay" } } };
    plot("heatBase", heat(w.grid.baseline, false), hl);
    plot("heat", heat(w.grid.optimized, true), hl);
    plot("whBullet", [
      { type: "bar", orientation: "h", name: "Measured", y: ["Pick rate gain", "Walking reduction"], x: [m.thr * 100, m.travel * 100], marker: { color: C.teal },
        text: [pct(m.thr), pct(m.travel)], textposition: "inside", textfont: { color: "#fff" } },
      { type: "bar", orientation: "h", name: "Ceiling", y: ["Pick rate gain", "Walking reduction"], x: [c.perfect_info_throughput_gain_pct, c.perfect_info_travel_reduction_pct], marker: { color: C.fog } },
      { type: "scatter", mode: "markers", name: "Target", y: ["Pick rate gain", "Walking reduction"], x: [25, 35],
        marker: { symbol: "line-ns", size: 30, line: { width: 3, color: C.breach } } },
    ], { barmode: "group", margin: { l: 120, r: 16, t: 4, b: 30 }, xaxis: { ticksuffix: "%", range: [0, 40], gridcolor: css("--rule", "#DCE1E8") } });
    $("whNote").textContent = "Measured on the most recent third of pick orders, which the layout never saw. Picking uses batches of three orders and return routing (walk into each aisle and back).";
    plot("pph", [{ type: "bar", x: ["Current", "Optimized", "Ceiling"], y: [pr.baseline.picks_per_hour, pr.optimized.picks_per_hour, pr.perfect_information.picks_per_hour],
      marker: { color: [C.fog, C.teal, C.slate] }, text: [pr.baseline, pr.optimized, pr.perfect_information].map((x) => x.picks_per_hour.toFixed(1)), textposition: "outside", cliponaxis: false }],
      { yaxis: { title: { text: "pick lines per hour" }, gridcolor: css("--rule", "#DCE1E8") }, margin: { l: 58, r: 10, t: 16, b: 30 } });
    $("whStats").innerHTML = [["Walking per pick line", `${pr.optimized.travel_m_per_line.toFixed(1)} m (was ${pr.baseline.travel_m_per_line.toFixed(1)})`],
      ["Picks from upper levels", `${pct(pr.optimized.upper_level_pick_share, 0)} (was ${pct(pr.baseline.upper_level_pick_share, 0)})`]]
      .map(([l, v]) => `<div><dt>${l}</dt><dd>${v}</dd></div>`).join("");
  }

  /* ---------------------------------------------------------------- DC operations */
  function simulation() {
    const m = metrics(state.city), s = P.simulation[state.city], sim = m.sim, t = P.meta.target_cycle;
    const extra = sim[SCN.opt].cost - sim[SCN.base].cost;
    $("pageAnswer").innerHTML = sim[SCN.base].pooled_p95 > t
      ? `Today's roster dispatches the slowest 5% of orders in <b>${sim[SCN.base].pooled_p95.toFixed(0)} minutes</b>, over the 2-hour promise. A <b>${staffTxt(sim[SCN.opt].staff)}</b> roster brings it to ${sim[SCN.opt].pooled_p95.toFixed(0)} minutes for ${inrS(extra)} a day. At Diwali volume today's roster reaches <b>${sim[SCN.dwBase].pooled_p95.toFixed(0)} minutes</b>.`
      : `Today's roster already meets the 2-hour promise (${sim[SCN.base].pooled_p95.toFixed(0)} minutes at P95). At Diwali volume it reaches <b>${sim[SCN.dwBase].pooled_p95.toFixed(0)} minutes</b>; a <b>${staffTxt(sim[SCN.dwPeak].staff)}</b> peak roster holds ${sim[SCN.dwPeak].pooled_p95.toFixed(0)}.`;
    tiles([
      { l: "P95 dispatch time, today's roster", v: `${sim[SCN.base].pooled_p95.toFixed(0)} min`, c: "promise 120 min", s: m.s.p95 },
      { l: "P95 with recommended roster", v: `${sim[SCN.opt].pooled_p95.toFixed(0)} min`, c: staffTxt(sim[SCN.opt].staff), s: m.s.p95opt },
      { l: "Extra staff cost", v: inrS(Math.max(0, extra)), c: "per day for the recommended roster" },
      { l: "Diwali P95, today's roster", v: `${sim[SCN.dwBase].pooled_p95.toFixed(0)} min`, c: `${int(sim[SCN.dwBase].backlog)} orders a day left over`, s: m.s.diwali },
      { l: "Diwali P95, peak roster", v: `${sim[SCN.dwPeak].pooled_p95.toFixed(0)} min`, c: staffTxt(sim[SCN.dwPeak].staff), s: lower(sim[SCN.dwPeak].pooled_p95, t, t * 1.25) },
      { l: "Bottleneck at Diwali", v: sim[SCN.dwBase].bottleneck[0].toUpperCase() + sim[SCN.dwBase].bottleneck.slice(1), c: "with today's roster" },
    ]);
    plot("simBar", [{ type: "bar", x: s.map((r) => SCN_LABEL[r.scenario]), y: s.map((r) => r.pooled_p95),
      marker: { color: s.map((r) => STATUS_COLOR[lower(r.pooled_p95, t, t * 1.25)]) }, text: s.map((r) => r.pooled_p95.toFixed(0) + " min"),
      textposition: "outside", cliponaxis: false, customdata: s.map((r) => staffTxt(r.staff)),
      hovertemplate: "%{x}<br>roster %{customdata}<br>P95 %{y:.0f} min<extra></extra>" }],
      { shapes: [targetLine("y", t)], annotations: [{ xref: "paper", x: 1, y: t, text: "2-hour promise", showarrow: false, yanchor: "bottom", xanchor: "right", font: { size: 11 } }],
        yaxis: { title: { text: "minutes" }, gridcolor: css("--rule", "#DCE1E8") }, margin: { l: 58, r: 10, t: 18, b: 40 } });
    const bins = s[0].hist.map((_, i) => i * 10);
    const col = { [SCN.base]: C.slate, [SCN.opt]: C.teal, [SCN.dwBase]: C.breach, [SCN.dwPeak]: C.watch };
    plot("simHist", s.map((r) => ({ type: "scatter", mode: "lines", name: SCN_LABEL[r.scenario], x: bins, y: r.hist,
      line: { color: col[r.scenario], width: 2, shape: "hv" } })),
      { shapes: [targetLine("x", t)], xaxis: { title: { text: "minutes from intake to dispatch" }, range: [0, 480] }, yaxis: { title: { text: "orders" }, gridcolor: css("--rule", "#DCE1E8") } });
    $("simTable").innerHTML = table(
      [{ t: "Scenario", l: 1 }, { t: "Roster" }, { t: "Staff cost a day" }, { t: "Orders dispatched" }, { t: "P95, typical day" }, { t: "Backlog" },
       { t: "Pickers", l: 1 }, { t: "Packers", l: 1 }, { t: "Loaders", l: 1 }, { t: "Bottleneck", l: 1 }, { t: "2-hour promise", l: 1 }],
      s.map((r) => `<tr><td class="l"><b>${SCN_LABEL[r.scenario]}</b></td><td>${staffTxt(r.staff)}</td><td>${inr(r.cost)}</td>
        <td>${int(r.thr[1])}<span class="sub">${int(r.thr[0])}–${int(r.thr[2])}</span></td><td>${r.p95[1].toFixed(0)} min<span class="sub">${r.p95[0].toFixed(0)}–${r.p95[2].toFixed(0)}</span></td>
        <td>${int(r.backlog)}</td>${[0, 1, 3].map((i) => `<td class="l">${bar(r.util[i], r.util[i] > 0.85)}${pct(r.util[i], 0)}</td>`).join("")}
        <td class="l">${r.bottleneck}</td><td class="l">${r.meets ? pill("good", "Meets") : pill("breach", "Misses")}</td></tr>`));
  }

  /* ---------------------------------------------------------------- network & finance */
  function npvOf(cf) {
    let v = -cf.capex;
    for (let t = 1; t <= P.meta.horizon; t++) v += (cf.saving * Math.pow(1 + state.growth, t - 1) - cf.opex - cf.linehaul) / Math.pow(1 + state.wacc, t);
    return v;
  }
  function breakEven(cf) {
    let pc = cf.capex, ps = 0;
    for (let t = 1; t <= P.meta.horizon; t++) {
      pc += (cf.opex + cf.linehaul) / Math.pow(1 + state.wacc, t);
      ps += (cf.saving * Math.pow(1 + state.growth, t - 1)) / Math.pow(1 + state.wacc, t);
    }
    return ps > 0 ? pc / ps : null;
  }
  function network() {
    const m = metrics(state.city), n = P.network.cities[state.city];
    const conf = n.configs.find((c) => c.p === state.p), today = n.configs.find((c) => c.p === 0);
    const chosen = new Set(conf.sites), v = npvOf(conf), be = breakEven(conf), rank = m.rank;
    $("pageAnswer").innerHTML = `${esc(cityName(state.city))} ships about <b>${int(rank.parcels_per_day)} parcels a day</b>, and today's network reaches <b>${pct(rank.baseline_sla_2h, 0)}</b> of them within two hours at evening-peak traffic. Opening ${state.p} micro-fulfilment centre${state.p > 1 ? "s" : ""} is worth <b>${inrS(v)}</b> over five years at these inputs; ${be ? `it breaks even at <b>${be.toFixed(1)}×</b> today's volume` : "it does not break even"}.`;
    tiles([
      { l: "Parcels a day", v: int(rank.parcels_per_day), c: "eligible for an MFC" },
      { l: "2-hour coverage today", v: pct(rank.baseline_sla_2h, 0), c: `${pct(conf.sla, 0)} with ${state.p} new site${state.p > 1 ? "s" : ""}`, s: m.s.sla },
      { l: "Five-year NPV", v: inrS(v), c: `${state.p} site${state.p > 1 ? "s" : ""}, WACC ${pct(state.wacc, 1)}`, s: v >= 0 ? "good" : "breach", st: v >= 0 ? "Positive" : "Negative" },
      { l: "Break-even volume", v: be ? `${be.toFixed(1)}×` : "n/a", c: be ? `about ${int(be * rank.parcels_per_day)} parcels a day` : "" },
      { l: "Km per parcel", v: conf.km.toFixed(1), c: `today ${today.km.toFixed(1)} km` },
      { l: "Investment priority", v: `${rank.investment_priority} of 5`, c: "by break-even volume" },
    ]);
    const center = P.meta.cities.find((x) => x.id === state.city).center;
    const dep = P.routing.depots[state.city];
    Plotly.react($("netMap"), [
      mapTrace(n.sites.map((s) => s[2]), n.sites.map((s) => s[3]), { mode: "markers+text", name: "Candidate sites",
        marker: { size: n.sites.map((s) => (chosen.has(s[0]) ? 18 : 10)), color: n.sites.map((s) => (chosen.has(s[0]) ? C.teal : C.watch)) },
        text: n.sites.map((s) => (chosen.has(s[0]) ? s[1] : "")), textposition: "bottom center", textfont: { color: C.teal, size: 12 },
        hovertext: n.sites.map((s) => `${s[1]}: rent ₹${s[4]} per sq ft a month${chosen.has(s[0]) ? " (selected)" : ""}`), hoverinfo: "text" }),
      mapTrace(dep.map((d) => d[3]), dep.map((d) => d[4]), { mode: "markers+text", text: dep.map((d) => d[1]), textposition: "top right",
        textfont: { size: 12, color: C.ink }, marker: { size: 13, color: C.ink, symbol: "square" }, name: "Existing depots", hoverinfo: "text" }),
    ], baseLayout(Object.assign({ margin: { l: 0, r: 0, t: 0, b: 0 }, legend: { x: 0.01, y: 0.99, bgcolor: "rgba(255,255,255,0.85)", orientation: "v" } }, mapLayout(center))), cfg);
    $("npvBox").innerHTML = [["Sites", esc(conf.names)], ["Up-front investment", inrS(conf.capex)], ["Running cost a year", inrS(conf.opex + conf.linehaul)],
      ["Distance saving in year 1", inrS(conf.saving)]].map(([l, x]) => `<div><dt>${l}</dt><dd>${x}</dd></div>`).join("");
    const flows = [-conf.capex];
    for (let t = 1; t <= P.meta.horizon; t++) flows.push(conf.saving * Math.pow(1 + state.growth, t - 1) - conf.opex - conf.linehaul);
    plot("cfWaterfall", [{ type: "waterfall", x: ["Year 0", ...flows.slice(1).map((_, i) => `Year ${i + 1}`), "Cumulative"],
      measure: [...flows.map(() => "relative"), "total"], y: [...flows.map((f) => f / 1e7), 0],
      decreasing: { marker: { color: C.breach } }, increasing: { marker: { color: C.good } }, totals: { marker: { color: C.ink } },
      text: [...flows.map((f) => (f / 1e7).toFixed(2)), ""], textposition: "outside", cliponaxis: false,
      connector: { line: { color: css("--rule", "#DCE1E8") } }, hovertemplate: "%{x}: ₹%{y:.2f} cr<extra></extra>" }],
      { yaxis: { title: { text: "₹ crore, undiscounted" }, gridcolor: css("--rule", "#DCE1E8") }, margin: { l: 58, r: 10, t: 14, b: 30 }, showlegend: false });
    const cfs = n.configs.filter((x) => x.p > 0);
    plot("npvBars", [{ type: "bar", x: cfs.map((x) => `${x.p}`), y: cfs.map((x) => npvOf(x) / 1e7),
      marker: { color: cfs.map((x) => (x.p === state.p ? C.ink : npvOf(x) >= 0 ? C.good : C.fog)) }, hovertemplate: "%{x} sites: ₹%{y:.2f} cr<extra></extra>" }],
      { xaxis: { title: { text: "new sites" }, dtick: 1, type: "category" }, yaxis: { title: { text: "₹ crore" }, gridcolor: css("--rule", "#DCE1E8"), zeroline: true, zerolinecolor: C.slate }, margin: { l: 58, r: 10, t: 10, b: 40 } });
    plot("netCurve", [
      { type: "scatter", mode: "lines+markers", name: "Km per parcel", x: n.configs.map((x) => x.p), y: n.configs.map((x) => x.km), line: { color: C.teal, width: 2.5 } },
      { type: "scatter", mode: "lines+markers", name: "2-hour coverage", x: n.configs.map((x) => x.p), y: n.configs.map((x) => x.sla), yaxis: "y2", line: { color: C.watch, width: 2.5 } },
    ], { xaxis: { title: { text: "new sites (0 = today's network)" }, dtick: 1 }, yaxis: { title: { text: "km" }, gridcolor: css("--rule", "#DCE1E8") },
         yaxis2: { overlaying: "y", side: "right", tickformat: ".0%", range: [0, 1.04], showgrid: false }, margin: { l: 58, r: 48, t: 30, b: 40 } });
    $("rankTable").innerHTML = table(
      [{ t: "Priority" }, { t: "City", l: 1 }, { t: "State(s)", l: 1 }, { t: "Parcels / day" }, { t: "2-hour coverage" }, { t: "Best option", l: 1 }, { t: "NPV at base case" }, { t: "Breaks even at" }],
      P.network.ranking.map((r) => `<tr class="clickable${r.city_id === state.city ? " sel" : ""}" data-city="${r.city_id}"><td>${r.investment_priority}</td>
        <td class="l"><b>${esc(r.city)}</b></td><td class="l">${esc(r.state)}</td><td>${int(r.parcels_per_day)}</td>
        <td class="${higher(r.baseline_sla_2h, 0.9, 0.75)}">${pct(r.baseline_sla_2h, 0)}</td><td class="l">${r.best_p} site: ${esc(r.best_sites)}</td>
        <td>${inrS(r.best_npv)}</td><td>${r.break_even_parcels_per_day ? int(r.break_even_parcels_per_day) + " parcels/day" : "n/a"}<span class="sub">${r.break_even_multiplier ? r.break_even_multiplier.toFixed(1) + "× today" : ""}</span></td></tr>`));
  }

  const RENDER = { overview, routes, fleet, warehouse, simulation, network };
  function render() {
    syncShell();
    RENDER[state.tab]();
  }

  async function init() {
    if (window.__PAYLOAD) {
      P = window.__PAYLOAD;
    } else {
      try {
        const res = await fetch("data/payload.json", { cache: "no-cache" });
        if (!res.ok) throw new Error(`data/payload.json returned ${res.status}`);
        P = await res.json();
      } catch (err) {
        $("main").innerHTML = `<div class="panel"><h2>Dashboard data is missing</h2><p>Build it with <code>python -m src.logistics.reporting.payload</code>, then serve the <code>dashboard/static</code> folder. Details: ${esc(err.message)}</p></div>`;
        return;
      }
    }
    state.city = P.meta.cities[0].id;
    state.wacc = P.meta.wacc;
    state.growth = P.meta.growth;
    $("waccSlider").value = state.wacc * 100;
    $("growthSlider").value = state.growth * 100;
    const h = location.hash.slice(1);
    if (TABS.includes(h)) state.tab = h;
    buildShell();
    render();
    window.__dashboardReady = true;
  }
  init().catch((err) => {
    console.error("Dashboard failed to initialise:", err && err.stack ? err.stack : err);
    window.__dashboardError = String(err);
  });
})();
