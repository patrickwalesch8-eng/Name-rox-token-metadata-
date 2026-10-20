/* Aktien Data Farm dashboard - reads window.FARM (data.js), no build step. */
(function () {
  "use strict";
  const F = window.FARM;
  const $ = (s, el = document) => el.querySelector(s);
  const view = $("#view");
  if (!F) { $("#nodata").hidden = false; return; }
  if (F.meta.demo) $("#demo-banner").hidden = false;

  // ---------- helpers
  const fmtDay = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" });
  const fmtTime = new Intl.DateTimeFormat("de-DE", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit" });
  const dayOf = t => fmtDay.format(new Date(t * 1000));
  const hm = t => fmtTime.format(new Date(t * 1000));
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const px = v => (v == null ? "–" : (+v).toFixed(2));
  const rr = v => (v == null ? "–" : (v > 0 ? "+" : "") + (+v).toFixed(2) + "R");
  const cls = v => (v > 0 ? "up" : v < 0 ? "down" : "dim");
  const pct = v => (v == null ? "–" : (v * 100).toFixed(0) + "%");
  const n0 = v => (v || 0).toLocaleString("de-DE");

  const KIND_DE = {
    session_open: "Session-Open", swing: "Swing-Punkt", pool_formed: "Liquiditäts-Pool", sweep: "Liquidity Sweep",
    pool_taken: "Pool durchbrochen", bos: "Break of Structure", mss: "Market Structure Shift",
    fvg_formed: "Fair Value Gap", fvg_tapped: "FVG angelaufen", fvg_inverted: "FVG invertiert",
    ob_formed: "Order Block", ob_mitigated: "OB angelaufen", ob_invalidated: "OB ungültig",
  };
  const REASON_DE = {
    kein_mss: "kein MSS nach dem Sweep", ersetzt: "durch neueren Sweep ersetzt", keine_zone: "keine FVG/OB-Zone",
    position_offen: "Position bereits offen", tageslimit: "Tageslimit erreicht", zeitfenster: "außerhalb Handelszeitfenster",
    gegen_htf_trend: "gegen 1h-Trend", zone_schon_erreicht: "Kurs schon in der Zone", stop_ungueltig: "Stop ungültig",
    risiko_zu_klein: "Risiko zu klein", risiko_zu_gross: "Risiko zu groß", lief_ohne_retest: "lief ohne Retest zum Ziel",
    kein_retest: "kein Retest der Zone", sessionende: "Session-Ende vor Einstieg",
  };
  const WHY_DE = { tp: "Ziel", sl: "Stop", eod: "Schluss" };

  // ---------- indexes
  const evById = new Map();
  const evBySymDay = {};
  for (const e of F.events) {
    evById.set(e.id, e);
    const d = dayOf(e.t);
    ((evBySymDay[e.s] ||= {})[d] ||= []).push(e);
  }
  const tradeById = new Map(F.trades.map(t => [t.id, t]));
  const barsDayCache = {};
  function dayBars(sym, tf, day) {
    const key = sym + "|" + tf;
    if (!barsDayCache[key]) {
      const idx = {};
      (F.bars[sym]?.[tf] || []).forEach((b, i) => { (idx[dayOf(b[0])] ||= []).push(b); });
      barsDayCache[key] = idx;
    }
    return barsDayCache[key][day] || [];
  }
  const days = F.meta.days;

  // ---------- routing
  const state = { tab: "overview", sym: F.meta.symbols[0], day: days[days.length - 1], tf: "5m", sel: null,
    evPage: 0, evFilter: { s: "", tf: "", k: "", d: "" } };
  function go(tab, patch) {
    Object.assign(state, patch || {});
    state.tab = tab;
    const h = tab === "chart" ? `chart/${state.sym}/${state.day}/${state.tf}` : tab === "studies" ? `studies/${state.day}` : tab;
    if (location.hash.replace(/^#/, "") === h) render();
    else location.hash = h;
  }
  function readHash() {
    const p = location.hash.replace(/^#/, "").split("/");
    if (p[0] === "chart") Object.assign(state, { tab: "chart", sym: p[1] || state.sym, day: p[2] || state.day, tf: p[3] || state.tf });
    else if (p[0] === "studies") Object.assign(state, { tab: "studies", day: p[1] || state.day });
    else if (["overview", "events"].includes(p[0])) state.tab = p[0];
    render();
  }
  window.addEventListener("hashchange", readHash);
  document.querySelectorAll("#tabs a").forEach(a => a.addEventListener("click", () => go(a.dataset.tab)));

  const gen = new Date(F.meta.generated * 1000);
  $("#status").innerHTML = `<b>●</b> Stand ${gen.toLocaleString("de-DE")} · ${F.meta.symbols.join(" · ")}`;

  function render() {
    document.querySelectorAll("#tabs a").forEach(a => a.classList.toggle("on", a.dataset.tab === state.tab));
    stopChart();
    ({ overview: renderOverview, chart: renderChart, studies: renderStudies, events: renderEvents })[state.tab]();
  }

  // ---------- chain text for a trade / refusal
  function chain(x) {
    const dir = x.dir === "long" ? "LONG" : "SHORT";
    let s = `<b>${hm(x.fill_t || x.sweep_t || x.t)} ${x.s} 5m ${dir}</b> — Sweep ${x.sweep_lb} ${px(x.sweep_p)}`;
    if (x.sweep_x != null) s += ` (Docht ${px(x.sweep_x)}${x.sweep_vol ? `, Vol ${(+x.sweep_vol).toFixed(1)}x` : ""})`;
    if (x.mss_k) s += ` → ${x.mss_k.toUpperCase()} ${px(x.mss_p)}`;
    if (x.zone) s += ` → ${x.zone.type} ${px(x.zone.bot)}–${px(x.zone.top)}`;
    if (x.entry != null) s += ` → Entry ${px(x.entry)} · SL ${px(x.sl)} · TP ${px(x.tp)}`;
    if (x.why) s += ` → <span class="${cls(x.net)}">${WHY_DE[x.why]} ${rr(x.r)} (netto ${rr(x.net)})</span>`;
    if (x.reason) s += ` → <span class="dim">abgelehnt: ${REASON_DE[x.reason] || x.reason}</span>`;
    return s;
  }

  // ================= OVERVIEW
  function renderOverview() {
    const S = F.stats, A = S.all;
    const nEvents = F.events.length;
    view.innerHTML = `<div class="wrap">
      <div class="eyebrow">Beobachtungs-Korpus · nur Analyse, kein Echtgeld</div>
      <h1>Was diese Farm beobachtet</h1>
      <p class="lead">Jeder Handelstag wird Bar für Bar abgespielt: Liquiditäts-Pools entstehen und werden gesweept,
      die Struktur dreht, Fair Value Gaps und Order Blocks entstehen und werden angelaufen – und die Engine
      entscheidet bei jedem Setup, ob sie es handelt oder ablehnt. Alles wird gespeichert.</p>
      <div class="kpis">
        ${kpi(days.length, "Tage abgedeckt")}${kpi(F.meta.symbols.length, "Werte")}
        ${kpi(n0(nEvents), "Events erkannt")}${kpi(A.n || 0, "Trades", "gold")}
        ${kpi(F.refusals.length, "Abgelehnt")}${kpi(pct(A.win), "Trefferquote")}
        ${kpi(rr(A.net), "Netto gesamt", cls(A.net))}${kpi(A.pf ?? "–", "Profit-Faktor")}
        ${kpi(A.dd ? "-" + A.dd + "R" : "0R", "Max. Drawdown", "down")}
      </div>
      <div class="grid2">
        <div class="card"><h2>Equity-Kurve <small>kumuliert in R · netto (gold) vs. brutto (grau)</small></h2>
          <canvas id="eq" style="width:100%;height:220px"></canvas>
          <p class="hint">1R = Betrag, der pro Trade riskiert wird. Bei 1&nbsp;% Risiko pro Trade entspricht +10R ≈ +10&nbsp;% Konto.</p></div>
        <div class="card"><h2>Warum abgelehnt?</h2><div class="bars">${refusalBars(S.refusals)}</div></div>
      </div>
      <div class="card scroll"><h2>Pro Wert</h2>${symbolTable(S)}</div>
      <div class="card scroll"><h2>Abdeckung <small>Events pro Tag · Klick öffnet den Chart</small></h2>${heatmap()}</div>
      <div class="card scroll"><h2>Outcome-Buch <small>alle Trades · Klick öffnet den Chart</small></h2>${tradeTable(F.trades.slice().reverse())}</div>
      <div class="card"><h2>Vokabular</h2><div class="vocab">${vocab()}</div></div>
    </div>`;
    drawEquity($("#eq"));
    view.querySelectorAll("[data-goto]").forEach(el => el.addEventListener("click", () => {
      const [sym, day] = el.dataset.goto.split("|");
      go("chart", { sym, day, tf: "5m" });
    }));
  }
  const kpi = (v, l, c = "") => `<div class="kpi"><div class="v ${c}">${v}</div><div class="l">${l}</div></div>`;

  function refusalBars(m) {
    const items = Object.entries(m || {}).sort((a, b) => b[1] - a[1]);
    const max = Math.max(1, ...items.map(x => x[1]));
    return items.map(([k, v]) => `<div class="row"><span>${REASON_DE[k] || k}</span><div class="bar" style="width:${(v / max) * 100}px"></div><span class="faint">${v}</span></div>`).join("") || `<p class="hint">keine</p>`;
  }

  function symbolTable(S) {
    const rows = Object.entries(S.by_symbol).map(([s, x]) => `<tr><td>${s}</td><td class="num">${x.n}</td><td class="num">${pct(x.win)}</td>
      <td class="num ${cls(x.avg)}">${rr(x.avg)}</td><td class="num ${cls(x.net)}">${rr(x.net)}</td><td class="num">${x.pf ?? "–"}</td><td class="num down">${x.dd ? "-" + x.dd + "R" : x.n ? "0R" : "–"}</td></tr>`);
    const d = S.by_dir;
    rows.push(`<tr><td class="dim">Long</td><td class="num">${d.long.n}</td><td class="num">${pct(d.long.win)}</td><td class="num">${rr(d.long.avg)}</td><td class="num ${cls(d.long.net)}">${rr(d.long.net)}</td><td></td><td></td></tr>`);
    rows.push(`<tr><td class="dim">Short</td><td class="num">${d.short.n}</td><td class="num">${pct(d.short.win)}</td><td class="num">${rr(d.short.avg)}</td><td class="num ${cls(d.short.net)}">${rr(d.short.net)}</td><td></td><td></td></tr>`);
    return `<table><tr><th>Wert</th><th class="num">Trades</th><th class="num">Treffer</th><th class="num">Ø netto</th><th class="num">Summe netto</th><th class="num">PF</th><th class="num">Max DD</th></tr>${rows.join("")}</table>`;
  }

  function heatmap() {
    const syms = F.meta.symbols;
    let max = 1;
    for (const s of syms) for (const d of days) max = Math.max(max, (evBySymDay[s]?.[d] || []).length);
    let html = `<div class="heat" style="grid-template-columns:60px repeat(${days.length}, minmax(10px,1fr))">`;
    for (const s of syms) {
      html += `<div class="lab">${s}</div>`;
      for (const d of days) {
        const n = (evBySymDay[s]?.[d] || []).length;
        const tr = F.studies[d]?.[s]?.trades?.length || 0;
        const a = n ? 0.15 + 0.85 * (n / max) : 0.04;
        html += `<div class="cell" data-goto="${s}|${d}" title="${s} ${d}: ${n} Events, ${tr} Trades"
          style="background:rgba(227,179,65,${a.toFixed(2)});${tr ? "outline:1px solid #2fbf71" : ""}"></div>`;
      }
    }
    return html + `</div><p class="hint">${days[0] || ""} … ${days[days.length - 1] || ""} · grün umrandet = Tag mit Trade</p>`;
  }

  function tradeTable(list) {
    if (!list.length) return `<p class="hint">Noch keine Trades – die Engine war bisher wählerisch.</p>`;
    return `<table><tr><th>Tag</th><th>Zeit</th><th>Wert</th><th>Richtung</th><th>Setup</th><th class="num">Entry</th><th class="num">SL</th><th class="num">TP</th><th class="num">Exit</th><th>Grund</th><th class="num">R</th><th class="num">netto</th></tr>
      ${list.map(t => `<tr class="click" data-goto="${t.s}|${t.day}"><td>${t.day}</td><td>${hm(t.fill_t)}</td><td>${t.s}</td>
        <td class="${t.dir === "long" ? "up" : "down"}">${t.dir.toUpperCase()}</td><td class="dim">${t.sweep_lb}→${t.mss_k.toUpperCase()}→${t.zone.type}</td>
        <td class="num">${px(t.entry)}</td><td class="num">${px(t.sl)}</td><td class="num">${px(t.tp)}</td><td class="num">${px(t.exit)}</td>
        <td>${WHY_DE[t.why]}</td><td class="num ${cls(t.r)}">${rr(t.r)}</td><td class="num ${cls(t.net)}">${rr(t.net)}</td></tr>`).join("")}</table>`;
  }

  function vocab() {
    const V = [
      ["Liquiditäts-Pool (EQH/EQL, PDH/PDL)", "Gleiche Hochs/Tiefs oder Vortageshoch/-tief. Darüber/darunter liegen Stopps – Liquidität, die große Teilnehmer abholen."],
      ["Sweep", "Der Kurs sticht mit dem Docht durch einen Pool und schließt wieder zurück: Stopps abgeholt, oft Startpunkt einer Umkehr."],
      ["Swing HH/HL/LH/LL", "Bestätigte Hoch-/Tiefpunkte. Höhere Hochs + höhere Tiefs = Aufwärtsstruktur."],
      ["BOS", "Break of Structure: Schlusskurs über/unter dem letzten Swing in Trendrichtung."],
      ["MSS", "Market Structure Shift: der erste Bruch gegen den bisherigen Trend – mögliche Umkehr."],
      ["FVG", "Fair Value Gap: Lücke zwischen Kerze 1 und 3 einer starken Bewegung. Wird oft erneut angelaufen."],
      ["Order Block", "Letzte Gegenkerze vor dem Strukturbruch – Zone, in der große Orders vermutet werden."],
      ["Setup der Engine", "Sweep → MSS innerhalb weniger Bars → Limit-Einstieg im FVG/OB → Stop hinter dem Sweep-Docht → Ziel 2R, sonst Ausstieg zum Schluss."],
      ["Brutto vs. netto", "Netto zieht Slippage/Gebühren ab (config.json). Nur netto zählt."],
    ];
    return V.map(([a, b]) => `<div><b>${a}</b><p>${b}</p></div>`).join("");
  }

  function drawEquity(cv) {
    const ctx = setupCanvas(cv);
    const W = cv.clientWidth, H = cv.clientHeight;
    const net = [0], gross = [0];
    F.trades.forEach(t => { net.push(net.at(-1) + t.net); gross.push(gross.at(-1) + t.r); });
    const lo = Math.min(...net, ...gross), hi = Math.max(...net, ...gross, 1);
    const x = i => 30 + (i / Math.max(1, net.length - 1)) * (W - 40);
    const y = v => 10 + (1 - (v - lo) / (hi - lo || 1)) * (H - 30);
    ctx.strokeStyle = "#26282d"; ctx.beginPath(); ctx.moveTo(30, y(0)); ctx.lineTo(W - 10, y(0)); ctx.stroke();
    ctx.fillStyle = "#5b5e64"; ctx.font = "10px monospace";
    ctx.fillText(hi.toFixed(1) + "R", 0, y(hi) + 4); ctx.fillText(lo.toFixed(1) + "R", 0, y(lo) + 4); ctx.fillText("0", 18, y(0) + 4);
    for (const [arr, col, dash] of [[gross, "#5b5e64", [4, 3]], [net, "#e3b341", []]]) {
      ctx.strokeStyle = col; ctx.setLineDash(dash); ctx.lineWidth = 1.5; ctx.beginPath();
      arr.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)))); ctx.stroke();
    }
    ctx.setLineDash([]);
    if (net.length < 2) { ctx.fillStyle = "#8a8d93"; ctx.fillText("noch keine Trades", W / 2 - 50, H / 2); }
  }

  function setupCanvas(cv) {
    const dpr = window.devicePixelRatio || 1;
    cv.width = cv.clientWidth * dpr; cv.height = cv.clientHeight * dpr;
    const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  }

  // ================= CHART
  const LAYERS = [
    ["pools", "Liquiditäts-Pools", "#4c8dff"], ["sweeps", "Sweeps", "#e3b341"], ["structure", "Swings HH/HL/LH/LL", "#d9dadc"],
    ["breaks", "BOS / MSS", "#e5534b"], ["fvg", "Fair Value Gaps", "#9b6bff"], ["ob", "Order Blocks", "#2fbf71"],
    ["trades", "Trades (Engine)", "#e3b341"], ["refusals", "Abgelehnte Setups", "#8a8d93"],
  ];
  const layerOn = Object.fromEntries(LAYERS.map(l => [l[0], true]));
  let chart = null;
  function stopChart() { if (chart) { window.removeEventListener("resize", chart.onResize); chart = null; } }

  function renderChart() {
    if (!days.includes(state.day)) state.day = days[days.length - 1];
    const di = days.indexOf(state.day);
    view.innerHTML = `<div class="chartbar">
        <select id="c-sym">${F.meta.symbols.map(s => `<option ${s === state.sym ? "selected" : ""}>${s}</option>`).join("")}</select>
        <button id="c-prev" ${di <= 0 ? "disabled" : ""}>◀</button>
        <select id="c-day">${days.map(d => `<option ${d === state.day ? "selected" : ""}>${d}</option>`).join("")}</select>
        <button id="c-next" ${di >= days.length - 1 ? "disabled" : ""}>▶</button>
        <span class="faint">TF</span>${F.meta.timeframes.map(tf => `<button class="tfb ${tf === state.tf ? "on" : ""}" data-tf="${tf}">${tf}</button>`).join("")}
        <button id="c-reset">Reset</button>
        <span class="hint" id="c-info"></span>
      </div>
      <div class="chartwrap">
        <div class="canvasbox" id="c-box"><canvas id="c-cv"></canvas><div class="ohlc" id="c-ohlc"></div></div>
        <div class="side">
          <div class="card"><div class="eyebrow">Inspektor</div><div id="c-insp" class="hint">Etwas im Chart anklicken – Zone, Marker, Trade.</div></div>
          <div class="card"><div class="eyebrow">Ebenen</div><div id="c-layers"></div></div>
          <p class="hint">Mausrad/Pinch = Zoom · Ziehen = verschieben · Pfeiltasten = Tage</p>
        </div>
      </div>`;
    $("#c-sym").onchange = e => go("chart", { sym: e.target.value });
    $("#c-day").onchange = e => go("chart", { day: e.target.value });
    $("#c-prev").onclick = () => go("chart", { day: days[di - 1] });
    $("#c-next").onclick = () => go("chart", { day: days[di + 1] });
    view.querySelectorAll(".tfb").forEach(b => (b.onclick = () => go("chart", { tf: b.dataset.tf })));

    const bars = dayBars(state.sym, state.tf, state.day);
    const evs = (evBySymDay[state.sym]?.[state.day] || []).filter(e => e.tf === state.tf);
    // zones formed on earlier days that are still alive today
    const dayStart = bars[0]?.[0] ?? 0, tfSec = { "5m": 300, "15m": 900, "30m": 1800, "1h": 3600 }[state.tf];
    const dayEnd = (bars.at(-1)?.[0] ?? 0) + tfSec;
    const prevDays = days.slice(Math.max(0, di - 1), di);
    for (const d of prevDays) for (const e of evBySymDay[state.sym]?.[d] || [])
      if (e.tf === state.tf && /_formed$/.test(e.k) && (e.t1 == null || e.t1 > dayStart)) evs.push(e);
    const trades = F.trades.filter(t => t.s === state.sym && t.day === state.day);
    const refs = F.refusals.filter(r => r.s === state.sym && r.day === state.day);
    $("#c-info").textContent = `${bars.length} Bars · ${evs.length} Events · ${trades.length} Trades · ${refs.length} abgelehnt`;

    const counts = {
      pools: evs.filter(e => e.k === "pool_formed").length, sweeps: evs.filter(e => e.k === "sweep").length,
      structure: evs.filter(e => e.k === "swing").length, breaks: evs.filter(e => e.k === "bos" || e.k === "mss").length,
      fvg: evs.filter(e => e.k === "fvg_formed").length, ob: evs.filter(e => e.k === "ob_formed").length,
      trades: trades.length, refusals: refs.length,
    };
    $("#c-layers").innerHTML = LAYERS.map(([k, l, c]) => `<label class="layer"><input type="checkbox" data-l="${k}" ${layerOn[k] ? "checked" : ""}>
      <i style="background:${c}"></i>${l}<span class="n">${counts[k]}</span></label>`).join("");
    $("#c-layers").querySelectorAll("input").forEach(cb => (cb.onchange = () => { layerOn[cb.dataset.l] = cb.checked; chart.draw(); }));

    if (!bars.length) { $("#c-insp").textContent = "Keine Bars für diesen Tag."; return; }
    chart = makeChart($("#c-cv"), bars, evs, trades, refs, dayStart, dayEnd);
    $("#c-reset").onclick = () => chart.reset();
    if (state.focus) { chart.select(state.focus); state.focus = null; }
  }

  function makeChart(cv, bars, evs, trades, refs, dayStart, dayEnd) {
    const box = cv.parentElement;
    let ctx, W, H, hits = [], hover = -1;
    const narrow = () => box.clientWidth < 600;
    let vn = narrow() ? Math.min(bars.length, 40) : bars.length; // visible window (bar indices)
    let vs = bars.length - vn;
    const RIGHT = 62, VOLH = 0.14;

    const idxOf = t => { // last bar with time <= t (fractional inside bar not needed)
      let lo = 0, hi = bars.length - 1;
      if (t <= bars[0][0]) return t < dayStart ? -0.5 : 0;
      if (t >= dayEnd) return bars.length - 0.5;
      while (lo < hi) { const m = (lo + hi + 1) >> 1; if (bars[m][0] <= t) lo = m; else hi = m - 1; }
      return lo;
    };
    function geom() {
      const bw = (W - RIGHT) / vn;
      const vis = bars.slice(Math.max(0, Math.floor(vs)), Math.ceil(vs + vn));
      let lo = Math.min(...vis.map(b => b[3])), hi = Math.max(...vis.map(b => b[2]));
      const pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad;
      const ph = H * (1 - VOLH) - 20;
      return {
        bw, lo, hi,
        x: i => (i - vs + 0.5) * bw,
        y: p => 10 + (1 - (p - lo) / (hi - lo)) * ph,
        vmax: Math.max(1, ...vis.map(b => b[5])),
      };
    }
    function draw() {
      ctx = setupCanvas(cv); W = cv.clientWidth; H = cv.clientHeight; hits = [];
      const g = geom();
      ctx.clearRect(0, 0, W, H);
      ctx.font = "10px ui-monospace, monospace";
      // price grid
      const step = niceStep((g.hi - g.lo) / 6);
      ctx.fillStyle = "#5b5e64"; ctx.strokeStyle = "#1c1e22";
      for (let p = Math.ceil(g.lo / step) * step; p < g.hi; p += step) {
        const y = g.y(p); ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W - RIGHT, y); ctx.stroke();
        ctx.fillText(p.toFixed(2), W - RIGHT + 4, y + 3);
      }
      // time labels
      const every = Math.max(1, Math.round(80 / g.bw));
      for (let i = Math.max(0, Math.floor(vs)); i < Math.min(bars.length, vs + vn); i++)
        if (i % every === 0) ctx.fillText(hm(bars[i][0]), g.x(i) - 14, H - 4);

      ctx.save(); ctx.beginPath(); ctx.rect(0, 0, W - RIGHT, H); ctx.clip();
      const xr = (t0, t1) => [g.x(idxOf(t0)) - g.bw / 2, g.x(t1 == null ? bars.length - 0.5 : idxOf(t1)) + g.bw / 2];
      // zones
      for (const e of evs) {
        if (e.k === "fvg_formed" && layerOn.fvg) zone(e, ...xr(e.t0, e.t1), e.d === "bull" ? "155,107,255" : "229,83,75", "FVG");
        if (e.k === "ob_formed" && layerOn.ob) zone(e, ...xr(e.t0, e.t1), e.d === "bull" ? "47,191,113" : "229,83,75", "OB");
      }
      function zone(e, x0, x1, rgb, tag) {
        const y0 = g.y(e.top), y1 = g.y(e.bot);
        ctx.fillStyle = `rgba(${rgb},${e.end === "inverted" || e.end === "invalidated" ? 0.07 : 0.16})`;
        ctx.fillRect(x0, y0, x1 - x0, Math.max(1, y1 - y0));
        ctx.strokeStyle = `rgba(${rgb},0.45)`; ctx.strokeRect(x0, y0, x1 - x0, Math.max(1, y1 - y0));
        ctx.fillStyle = `rgba(${rgb},0.9)`; ctx.fillText(tag, x0 + 2, y0 - 2);
        hits.push({ x0, x1, y0: y0 - 2, y1: y1 + 2, obj: e, area: (x1 - x0) * (y1 - y0) });
      }
      // pools
      if (layerOn.pools) for (const e of evs) if (e.k === "pool_formed") {
        const [x0, x1] = xr(e.t0, e.t1); const y = g.y(e.p);
        ctx.strokeStyle = e.end === "swept" ? "#e3b341" : "#4c8dff"; ctx.setLineDash(e.end === "expired" ? [3, 3] : []);
        ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = "#4c8dff"; ctx.fillText(`${e.lb} ${e.side}`, Math.max(2, x1 - 60), y - 3);
        hits.push({ x0, x1, y0: y - 4, y1: y + 4, obj: e, area: (x1 - x0) * 8 });
      }
      // candles
      for (let i = Math.max(0, Math.floor(vs)); i < Math.min(bars.length, vs + vn + 1); i++) {
        const [, o, h, l, c, v] = bars[i]; const x = g.x(i); const up = c >= o;
        ctx.strokeStyle = ctx.fillStyle = up ? "#2fbf71" : "#e5534b";
        ctx.beginPath(); ctx.moveTo(x, g.y(h)); ctx.lineTo(x, g.y(l)); ctx.stroke();
        const bw = Math.max(1, g.bw * 0.65), yo = g.y(Math.max(o, c)), hgt = Math.max(1, Math.abs(g.y(o) - g.y(c)));
        ctx.fillRect(x - bw / 2, yo, bw, hgt);
        ctx.globalAlpha = 0.35; const vh = (v / g.vmax) * H * VOLH; ctx.fillRect(x - bw / 2, H - 16 - vh, bw, vh); ctx.globalAlpha = 1;
      }
      // structure breaks
      if (layerOn.breaks) for (const e of evs) if (e.k === "bos" || e.k === "mss") {
        const x0 = g.x(idxOf(e.t0)), x1 = g.x(idxOf(e.t)), y = g.y(e.p);
        ctx.strokeStyle = e.k === "mss" ? "#e5534b" : "#8a8d93"; ctx.setLineDash([5, 3]);
        ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = ctx.strokeStyle; ctx.fillText(e.k.toUpperCase(), (x0 + x1) / 2 - 10, y + (e.d === "bull" ? -3 : 11));
        hits.push({ x0, x1, y0: y - 5, y1: y + 5, obj: e, area: 50 });
      }
      // swings
      if (layerOn.structure) for (const e of evs) if (e.k === "swing") {
        const x = g.x(idxOf(e.t0)), y = g.y(e.p) + (e.side === "H" ? -6 : 14);
        ctx.fillStyle = /HH|HL/.test(e.lb) ? "#2fbf71" : /LH|LL/.test(e.lb) ? "#e5534b" : "#8a8d93";
        ctx.fillText(e.lb, x - 6, y);
        hits.push({ x0: x - 8, x1: x + 8, y0: y - 10, y1: y + 2, obj: e, area: 10 });
      }
      // sweeps
      if (layerOn.sweeps) for (const e of evs) if (e.k === "sweep") {
        const x = g.x(idxOf(e.t)), y = g.y(e.x) + (e.d === "bear" ? -10 : 10);
        ctx.fillStyle = "#e3b341"; ctx.beginPath(); ctx.arc(x, y, 4, 0, 7); ctx.fill();
        ctx.fillText("Sweep", x + 6, y + 3);
        hits.push({ x0: x - 6, x1: x + 6, y0: y - 6, y1: y + 6, obj: e, area: 5 });
      }
      // refusals
      if (layerOn.refusals) for (const r of refs) {
        const x = g.x(idxOf(r.t)), y = 22;
        ctx.strokeStyle = "#8a8d93"; ctx.beginPath(); ctx.arc(x, y, 4, 0, 7); ctx.stroke();
        hits.push({ x0: x - 6, x1: x + 6, y0: y - 6, y1: y + 6, obj: r, area: 5 });
      }
      // trades
      if (layerOn.trades) for (const t of trades) {
        const x0 = g.x(idxOf(t.fill_t)) - g.bw / 2, x1 = g.x(idxOf(t.exit_t)) + g.bw / 2;
        const ye = g.y(t.entry), ys = g.y(t.sl), yt = g.y(t.tp);
        ctx.fillStyle = "rgba(229,83,75,0.18)"; ctx.fillRect(x0, Math.min(ye, ys), x1 - x0, Math.abs(ys - ye));
        ctx.fillStyle = "rgba(47,191,113,0.18)"; ctx.fillRect(x0, Math.min(ye, yt), x1 - x0, Math.abs(yt - ye));
        ctx.strokeStyle = "#e3b341"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x0, ye); ctx.lineTo(x1, ye); ctx.stroke(); ctx.lineWidth = 1;
        ctx.fillStyle = "#e3b341"; ctx.fillText(`★ ${t.dir.toUpperCase()} ${rr(t.net)}`, x0, Math.min(ye, ys, yt) - 4);
        hits.push({ x0, x1, y0: Math.min(ye, ys, yt), y1: Math.max(ye, ys, yt), obj: t, area: 1 });
      }
      ctx.restore();
      if (hover >= 0 && bars[hover]) {
        const b = bars[hover];
        $("#c-ohlc").innerHTML = `${state.sym} ${state.tf} · ${hm(b[0])} &nbsp;O ${px(b[1])} H ${px(b[2])} L ${px(b[3])} C <span class="${b[4] >= b[1] ? "up" : "down"}">${px(b[4])}</span> V ${n0(b[5])}`;
      }
    }
    function niceStep(x) { const p = Math.pow(10, Math.floor(Math.log10(x || 1))); const m = x / p; return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * p; }
    function clamp() { vn = Math.max(10, Math.min(bars.length, vn)); vs = Math.max(-2, Math.min(bars.length - vn + 2, vs)); }

    function inspect(obj) {
      let html = "";
      if (obj.fill_t != null) html = `<div class="chain">${chain(obj)}</div>`;
      else if (obj.reason) html = `<div class="chain">${chain(obj)}</div>`;
      else html = `<b class="up">${KIND_DE[obj.k] || obj.k}</b> <span class="faint">${obj.tf} · ${hm(obj.t)}</span>`;
      $("#c-insp").innerHTML = html + `<pre class="json">${esc(JSON.stringify(obj, null, 1))}</pre>`;
    }

    // interaction
    let drag = null, pinch = null;
    const barAt = mx => Math.floor(vs + mx / ((W - RIGHT) / vn));
    cv.addEventListener("wheel", ev => {
      ev.preventDefault();
      const mx = ev.offsetX, anchor = vs + mx / ((W - RIGHT) / vn);
      const f = ev.deltaY > 0 ? 1.15 : 1 / 1.15; const nn = vn * f;
      vs = anchor - (anchor - vs) * (nn / vn); vn = nn; clamp(); draw();
    }, { passive: false });
    cv.addEventListener("pointerdown", ev => { drag = { x: ev.clientX, vs, moved: false }; cv.setPointerCapture(ev.pointerId); });
    cv.addEventListener("pointermove", ev => {
      if (drag && !pinch) {
        const dx = ev.clientX - drag.x; if (Math.abs(dx) > 3) drag.moved = true;
        vs = drag.vs - dx / ((W - RIGHT) / vn); clamp(); draw();
      } else { hover = barAt(ev.offsetX); draw(); }
    });
    cv.addEventListener("pointerup", ev => {
      if (drag && !drag.moved) {
        const r = cv.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
        const hit = hits.filter(h => mx >= h.x0 && mx <= h.x1 && my >= Math.min(h.y0, h.y1) && my <= Math.max(h.y0, h.y1))
          .sort((a, b) => a.area - b.area)[0];
        if (hit) inspect(hit.obj);
        hover = barAt(mx); draw();
      }
      drag = null;
    });
    cv.addEventListener("touchstart", ev => { if (ev.touches.length === 2) pinch = { d: dist(ev.touches), vn, vs }; }, { passive: true });
    cv.addEventListener("touchmove", ev => {
      if (pinch && ev.touches.length === 2) { const nn = pinch.vn * (pinch.d / dist(ev.touches)); vs = pinch.vs + (pinch.vn - nn) / 2; vn = nn; clamp(); draw(); }
    }, { passive: true });
    cv.addEventListener("touchend", () => { if (pinch) { pinch = null; drag = null; } });
    const dist = t => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
    const onResize = () => draw();
    window.addEventListener("resize", onResize);

    draw();
    return {
      draw, onResize,
      reset() { vn = narrow() ? Math.min(bars.length, 40) : bars.length; vs = bars.length - vn; draw(); },
      select(id) { const o = tradeById.get(id) || evById.get(id); if (o) inspect(o); },
    };
  }
  document.addEventListener("keydown", e => {
    if (state.tab !== "chart" || e.target.tagName === "SELECT") return;
    const di = days.indexOf(state.day);
    if (e.key === "ArrowLeft" && di > 0) go("chart", { day: days[di - 1] });
    if (e.key === "ArrowRight" && di < days.length - 1) go("chart", { day: days[di + 1] });
  });

  // ================= STUDIES
  function renderStudies() {
    if (!days.includes(state.day)) state.day = days[days.length - 1];
    const desc = days.slice().reverse();
    let list = "", month = "";
    for (const d of desc) {
      if (d.slice(0, 7) !== month) { month = d.slice(0, 7); list += `<div class="m">${month}</div>`; }
      const nT = Object.values(F.studies[d] || {}).reduce((a, s) => a + s.trades.length, 0);
      list += `<a data-day="${d}" class="${d === state.day ? "on" : ""}">${d}${nT ? ` <span class="up">★${nT}</span>` : ""}</a>`;
    }
    const st = F.studies[state.day] || {};
    const di = days.indexOf(state.day);
    let body = `<div style="display:flex;justify-content:space-between" class="hint">
        <a data-day="${days[di - 1] || ""}">${days[di - 1] ? "← " + days[di - 1] : ""}</a><a data-day="${days[di + 1] || ""}">${days[di + 1] ? days[di + 1] + " →" : ""}</a></div>
      <h1>Farm-Studie — ${state.day}</h1>
      <p class="hint">Automatisch erzeugt aus allen Events dieses Tages. R-Werte brutto und netto (Slippage ${F.meta.config.costs.slippage_bps} bps je Seite).</p>`;
    for (const sym of F.meta.symbols) {
      const s = st[sym]; if (!s) continue;
      const trades = s.trades.map(id => tradeById.get(id));
      const refs = F.refusals.filter(r => r.s === sym && r.day === state.day);
      const byKind = {};
      for (const [k, n] of Object.entries(s.counts)) { const [kind, tf] = k.split("@"); (byKind[kind] ||= {})[tf] = n; }
      body += `<div class="card"><h2><a data-goto="${sym}">${sym}</a> <small>${trades.length} Trades · ${refs.length} abgelehnt</small></h2>
        <div class="counts">${Object.entries(byKind).sort().map(([k, m]) => `<div><span>${k}</span><span class="dim">${F.meta.timeframes.map(tf => m[tf] || 0).join(" / ")}</span></div>`).join("")}</div>
        <p class="hint">Anzahl je Timeframe: ${F.meta.timeframes.join(" / ")}</p>
        ${trades.length ? `<h2 style="margin-top:12px">★ Trades</h2>${trades.map(t => `<div class="chain">${chain(t)}</div>`).join("")}` : ""}
        ${refs.length ? `<h2 style="margin-top:12px">Abgelehnte Setups</h2>${refs.map(r => `<div class="chain dim">${chain(r)}</div>`).join("")}` : ""}
      </div>`;
    }
    view.innerHTML = `<div class="wrap studies"><div class="daylist">${list}</div><div>${body}</div></div>`;
    view.querySelectorAll("[data-day]").forEach(a => a.dataset.day && (a.onclick = () => go("studies", { day: a.dataset.day })));
    view.querySelectorAll("[data-goto]").forEach(a => (a.onclick = () => go("chart", { sym: a.dataset.goto, day: state.day, tf: "5m" })));
  }

  // ================= EVENTS
  function renderEvents() {
    const f = state.evFilter;
    const kinds = Object.keys(F.stats.events).sort();
    const rows = F.events.filter(e => (!f.s || e.s === f.s) && (!f.tf || e.tf === f.tf) && (!f.k || e.k === f.k) && (!f.d || dayOf(e.t) === f.d));
    const PER = 100, pages = Math.max(1, Math.ceil(rows.length / PER));
    state.evPage = Math.min(state.evPage, pages - 1);
    const page = rows.slice().reverse().slice(state.evPage * PER, state.evPage * PER + PER);
    const opt = (arr, v, all) => `<option value="">${all}</option>` + arr.map(x => `<option ${x === v ? "selected" : ""}>${x}</option>`).join("");
    view.innerHTML = `<div class="wrap">
      <div class="filters">
        <select data-f="s">${opt(F.meta.symbols, f.s, "alle Werte")}</select>
        <select data-f="tf">${opt(F.meta.timeframes, f.tf, "alle TF")}</select>
        <select data-f="k">${opt(kinds, f.k, "alle Arten")}</select>
        <select data-f="d">${opt(days.slice().reverse(), f.d, "alle Tage")}</select>
        <span class="hint" style="align-self:center">${n0(rows.length)} Events</span>
      </div>
      <div class="card scroll"><table><tr><th>Tag</th><th>Zeit</th><th>Wert</th><th>TF</th><th>Art</th><th>Richtung</th><th>Details</th></tr>
      ${page.map(e => `<tr class="click" data-id="${e.id}"><td>${dayOf(e.t)}</td><td>${hm(e.t)}</td><td>${e.s}</td><td>${e.tf}</td>
        <td>${e.k}</td><td class="${e.d === "bull" ? "up" : e.d === "bear" ? "down" : "dim"}">${e.d || e.side || ""}</td>
        <td class="dim">${detail(e)}</td></tr>`).join("")}</table>
      <div class="pager"><button id="pg-prev" ${state.evPage ? "" : "disabled"}>◀</button>Seite ${state.evPage + 1} / ${pages}<button id="pg-next" ${state.evPage < pages - 1 ? "" : "disabled"}>▶</button></div></div></div>`;
    view.querySelectorAll("[data-f]").forEach(s => (s.onchange = () => { f[s.dataset.f] = s.value; state.evPage = 0; renderEvents(); }));
    $("#pg-prev").onclick = () => { state.evPage--; renderEvents(); };
    $("#pg-next").onclick = () => { state.evPage++; renderEvents(); };
    view.querySelectorAll("tr[data-id]").forEach(tr => (tr.onclick = () => {
      const e = evById.get(tr.dataset.id);
      go("chart", { sym: e.s, day: dayOf(e.t), tf: e.tf, focus: e.id });
    }));
  }
  function detail(e) {
    if (e.top != null) return `${px(e.bot)}–${px(e.top)}${e.end ? " · " + e.end : ""}`;
    if (e.k === "sweep") return `${e.lb} ${px(e.p)} · Docht ${px(e.x)} · Vol ${e.vol}x`;
    if (e.k === "pool_formed") return `${e.lb} ${px(e.p)}${e.end ? " · " + e.end : ""}`;
    if (e.p != null) return `${e.lb || ""} ${px(e.p)}`;
    return "";
  }

  readHash();
})();
