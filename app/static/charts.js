// 簡單的 SVG 圖表（統計頁、查詢結果的建物價格圖共用）：折線（可加分位數色帶、散點）、直條；滑過顯示數值。
// 不依賴外部函式庫；寬度跟著容器，容器尺寸改變時重畫。

const NS = "http://www.w3.org/2000/svg";
const PAD = { l: 44, r: 12, t: 10, b: 24 };

function niceTicks(lo, hi, n = 4) {
  if (!(hi > lo)) { hi = lo + 1; }
  const raw = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
  const out = [];  // 刻度一定包住資料：從不大於 lo 的刻度到不小於 hi 的刻度
  for (let v = Math.floor(lo / step) * step; v < hi + step - step * 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

function tipBox(host) {
  let tip = host.querySelector(".chart-tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "chart-tip";
    tip.style.cssText = "position:absolute;pointer-events:none;background:rgba(15,23,42,.92);color:#fff;font-size:12px;"
      + "line-height:1.5;padding:6px 8px;border-radius:6px;white-space:nowrap;display:none;z-index:5";
    host.appendChild(tip);
  }
  return tip;
}

// 時間軸的折線圖。
// series: [{ name, color, points: [[x, y], ...], dash, dots, band: [[x, lo, hi], ...], faintFrom }]
//   x 為毫秒時間；dots＝只畫點不連線；faintFrom＝這個時間之後的點畫成空心（例：資料未齊的期間）
// opts: { height, yFmt(v), xFmt(x), tipFmt(x) , yZero }
export function lineChart(host, series, opts = {}) {
  const draw = () => {
    host.style.position = "relative";
    host.querySelectorAll("svg").forEach(s => s.remove());
    const W = Math.max(host.clientWidth, 240), H = opts.height || 220;
    const xs = [], ys = [];
    for (const s of series) {
      for (const [x, y] of s.points) { xs.push(x); ys.push(y); }
      for (const [x, lo, hi] of s.band || []) { xs.push(x); ys.push(lo, hi); }
    }
    if (!xs.length) return;
    let [x0, x1] = [Math.min(...xs), Math.max(...xs)];
    if (x0 === x1) { x0 -= 864e5 * 180; x1 += 864e5 * 180; }
    const yt = niceTicks(opts.yZero ? 0 : Math.min(...ys) * 0.95, Math.max(...ys) * 1.03);
    const [y0, y1] = [yt[0], yt[yt.length - 1]];
    const sx = x => PAD.l + (x - x0) / (x1 - x0) * (W - PAD.l - PAD.r);
    const sy = y => PAD.t + (1 - (y - y0) / (y1 - y0)) * (H - PAD.t - PAD.b);
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img",
      "aria-label": opts.label || "", style: "display:block;overflow:visible;font:11px -apple-system,sans-serif" }, host);
    for (const v of yt) {
      el("line", { x1: PAD.l, x2: W - PAD.r, y1: sy(v), y2: sy(v), stroke: "#e2e8f0" }, svg);
      el("text", { x: PAD.l - 6, y: sy(v) + 4, "text-anchor": "end", fill: "#64748b" }, svg).textContent = (opts.yFmt || String)(v);
    }
    // x 軸：年份刻度（太密就隔年）
    const ya = new Date(x0).getUTCFullYear(), yb = new Date(x1).getUTCFullYear();
    const every = Math.max(1, Math.ceil((yb - ya + 1) / Math.max(2, Math.floor((W - PAD.l) / 48))));
    for (let y = ya; y <= yb + 1; y += every) {
      const x = Date.UTC(y, 0, 1);
      if (x < x0 - 1 || x > x1 + 1) continue;
      el("line", { x1: sx(x), x2: sx(x), y1: H - PAD.b, y2: H - PAD.b + 4, stroke: "#94a3b8" }, svg);
      el("text", { x: sx(x), y: H - 6, "text-anchor": "middle", fill: "#64748b" }, svg).textContent = y;
    }
    for (const s of series) {
      if (s.band?.length) {
        const top = s.band.map(([x, , hi]) => `${sx(x)},${sy(hi)}`), bot = s.band.map(([x, lo]) => `${sx(x)},${sy(lo)}`).reverse();
        el("polygon", { points: [...top, ...bot].join(" "), fill: s.color, opacity: 0.12 }, svg);
      }
      const pts = s.points;
      if (!s.dots && pts.length > 1) {
        const solid = pts.filter(([x]) => s.faintFrom == null || x <= s.faintFrom);
        const faint = s.faintFrom == null ? [] : pts.filter(([x]) => x >= s.faintFrom || x === solid.at(-1)?.[0]);
        const line = (ps, dash) => ps.length > 1 && el("polyline", { points: ps.map(([x, y]) => `${sx(x)},${sy(y)}`).join(" "),
          fill: "none", stroke: s.color, "stroke-width": s.width || 2, "stroke-dasharray": dash, "stroke-linejoin": "round" }, svg);
        line(solid, s.dash);
        line(faint, "3 3");
      }
      const r = s.dots ? 3.2 : (pts.length <= 24 ? 2.6 : 0);
      if (r) for (const [x, y] of pts) {
        const faint = s.faintFrom != null && x > s.faintFrom;
        el("circle", { cx: sx(x), cy: sy(y), r, fill: faint ? "#fff" : s.color, stroke: s.color,
          "stroke-width": faint ? 1.5 : 0, opacity: s.dots ? 0.75 : 1 }, svg);
      }
    }
    // 滑過：最近的時間點，列出各系列在那個時間的值
    const guide = el("line", { y1: PAD.t, y2: H - PAD.b, stroke: "#94a3b8", "stroke-dasharray": "2 2", visibility: "hidden" }, svg);
    const tip = tipBox(host);
    const allX = [...new Set(series.filter(s => !s.dots).flatMap(s => s.points.map(p => p[0])))].sort((a, b) => a - b);
    const move = ev => {
      if (!allX.length) return;
      const rect = svg.getBoundingClientRect(), px = (ev.clientX - rect.left) * (W / rect.width);
      const x = allX.reduce((a, b) => Math.abs(sx(b) - px) < Math.abs(sx(a) - px) ? b : a);
      const rows = series.filter(s => !s.dots && s.name).map(s => {
        const p = s.points.find(q => q[0] === x);
        return p ? `<span style="color:${s.color}">●</span> ${escHtml(s.name)}：${(opts.tipFmt || opts.yFmt || String)(p[1], p)}` : null;
      }).filter(Boolean);
      if (!rows.length) return;
      guide.setAttribute("x1", sx(x)); guide.setAttribute("x2", sx(x)); guide.setAttribute("visibility", "visible");
      tip.innerHTML = `<b>${(opts.xFmt || (v => new Date(v).getUTCFullYear()))(x)}</b><br>${rows.join("<br>")}`;
      tip.style.display = "block";
      const left = sx(x) / W * rect.width;
      tip.style.left = `${Math.min(Math.max(left - tip.offsetWidth / 2, 0), rect.width - tip.offsetWidth)}px`;
      tip.style.top = `${-tip.offsetHeight - 4}px`;
    };
    const leave = () => { tip.style.display = "none"; guide.setAttribute("visibility", "hidden"); };
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerdown", move);
    svg.addEventListener("pointerleave", leave);
  };
  draw();
  watchWidth(host, draw);
}

// 時間軸的直條圖（成交量）。bars: [[x, n, faint]]；opts: { color, xFmt, height }
export function barChart(host, bars, opts = {}) {
  const draw = () => {
    host.style.position = "relative";
    host.querySelectorAll("svg").forEach(s => s.remove());
    if (!bars.length) return;
    const W = Math.max(host.clientWidth, 240), H = opts.height || 110;
    const yt = niceTicks(0, Math.max(...bars.map(b => b[1])), 2);
    const top = yt[yt.length - 1];
    const bw = (W - PAD.l - PAD.r) / bars.length;
    const sy = y => PAD.t + (1 - y / top) * (H - PAD.t - PAD.b);
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img", "aria-label": opts.label || "",
      style: "display:block;overflow:visible;font:11px -apple-system,sans-serif" }, host);
    for (const v of yt) {
      el("line", { x1: PAD.l, x2: W - PAD.r, y1: sy(v), y2: sy(v), stroke: "#e2e8f0" }, svg);
      el("text", { x: PAD.l - 6, y: sy(v) + 4, "text-anchor": "end", fill: "#64748b" }, svg).textContent = v.toLocaleString();
    }
    const tip = tipBox(host);
    let lastYear = null, lastLabel = -1e9;
    bars.forEach(([x, n, faint], i) => {
      const r = el("rect", { x: PAD.l + i * bw + bw * 0.15, width: Math.max(bw * 0.7, 1), y: sy(n), height: sy(0) - sy(n),
        fill: faint ? "#fff" : (opts.color || "#94a3b8"), stroke: faint ? (opts.color || "#94a3b8") : null, rx: 1.5 }, svg);
      const show = () => {
        tip.innerHTML = `<b>${(opts.xFmt || (v => new Date(v).getUTCFullYear()))(x)}</b><br>${n.toLocaleString()} 筆${faint ? "（資料未齊）" : ""}`;
        tip.style.display = "block";
        const rect = svg.getBoundingClientRect(), left = (PAD.l + (i + 0.5) * bw) / W * rect.width;
        tip.style.left = `${Math.min(Math.max(left - tip.offsetWidth / 2, 0), rect.width - tip.offsetWidth)}px`;
        tip.style.top = `${-tip.offsetHeight - 4}px`;
      };
      r.addEventListener("pointerenter", show);
      r.addEventListener("pointerdown", show);
      r.addEventListener("pointerleave", () => { tip.style.display = "none"; });
      const y = new Date(x).getUTCFullYear(), cx = PAD.l + (i + 0.5) * bw;
      if (y !== lastYear && cx - lastLabel >= 34) {  // 年份標籤至少隔 34px，太密就跳過
        el("text", { x: bars.length > 16 ? PAD.l + i * bw : cx, y: H - 6, "text-anchor": bars.length > 16 ? "start" : "middle",
          fill: "#64748b" }, svg).textContent = y;
        lastLabel = cx;
      }
      lastYear = y;
    });
  };
  draw();
  watchWidth(host, draw);
}

// 清掉圖表（例：切到沒有資料的分類）：停止監看寬度，否則視窗縮放時會把舊圖表畫回來
export function clearChart(host, html = "") {
  host._ro?.disconnect();
  host._ro = null;
  host.innerHTML = html;
}

const escHtml = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function watchWidth(host, draw) {
  host._ro?.disconnect();
  let w = host.clientWidth;
  host._ro = new ResizeObserver(() => { if (Math.abs(host.clientWidth - w) > 4) { w = host.clientWidth; draw(); } });
  host._ro.observe(host);
}

// 「2024-07-01」→ 毫秒（UTC）
export const ms = iso => Date.parse(iso + "T00:00:00Z");
// 元／坪 → 「32.5 萬」
export const wan = v => v >= 1e6 ? `${Math.round(v / 1e4)} 萬` : `${(v / 1e4).toFixed(1)} 萬`;
