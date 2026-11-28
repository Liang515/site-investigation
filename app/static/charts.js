// 簡單的 SVG 圖表（統計頁、查詢結果的建物價格圖共用）：折線（可加分位數色帶、散點）、直條；滑過／點擊顯示數值。
// 不依賴外部函式庫；寬度跟著容器，容器尺寸改變時重畫。顏色走 CSS 變數，自動支援淺／深色模式。

const NS = "http://www.w3.org/2000/svg";
const PAD = { l: 44, r: 12, t: 10, b: 24 };

// 點擊圖表外部時關閉浮動提示（觸控裝置上 pointerleave 不一定會觸發）
const activeDismiss = new Set();
let dismissWired = false;
function wireDismiss() {
  if (dismissWired) return;
  dismissWired = true;
  document.addEventListener("pointerdown", e => {
    for (const fn of activeDismiss) fn(e.target);
  });
}

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
    tip.style.cssText = "position:absolute;pointer-events:none;background:var(--tip-bg, rgba(15,23,42,.94));color:#fff;font-size:12px;"
      + "line-height:1.5;padding:6px 9px;border-radius:6px;white-space:nowrap;display:none;z-index:5;box-shadow:0 4px 12px rgba(0,0,0,.22)";
    host.appendChild(tip);
  }
  return tip;
}

// 提示框放在圖表內側（優先避開指標所在的那一側），不遮上方標題／圖例也不超出容器邊界
function placeTip(tip, rect, anchorX, anchorY) {
  tip.style.display = "block";
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  const gap = 10;
  let left = anchorX + gap;
  if (left + tw > rect.width - 2) left = anchorX - tw - gap;
  left = Math.min(Math.max(left, 2), Math.max(2, rect.width - tw - 2));
  let top = anchorY - th / 2;
  top = Math.min(Math.max(top, 4), Math.max(4, rect.height - th - 26));
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

// 時間軸的折線圖。
// series: [{ name, color, points: [[x, y, meta?], ...], dash, dots, band: [[x, lo, hi], ...], faintFrom }]
//   x 為毫秒時間；dots＝只畫點不連線（同樣可滑過／點擊看單筆）；faintFrom＝這個時間之後的點畫成空心（例：資料未齊的期間）
// opts: { height, yFmt(v), xFmt(x), tipFmt(v, p), dotFmt(p), yZero, label }
export function lineChart(host, series, opts = {}) {
  wireDismiss();
  if (host._dismiss) { activeDismiss.delete(host._dismiss); host._dismiss = null; }
  const draw = () => {
    if (host._dismiss) { activeDismiss.delete(host._dismiss); host._dismiss = null; }
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
      "aria-label": opts.label || "", style: "display:block;overflow:visible;font:11px -apple-system,sans-serif;touch-action:pan-y" }, host);
    for (const v of yt) {
      el("line", { x1: PAD.l, x2: W - PAD.r, y1: sy(v), y2: sy(v), stroke: "var(--line, #e2e8f0)" }, svg);
      el("text", { x: PAD.l - 6, y: sy(v) + 4, "text-anchor": "end", fill: "var(--muted, #64748b)" }, svg).textContent = (opts.yFmt || String)(v);
    }
    // x 軸：年份刻度（太密就隔年）
    const ya = new Date(x0).getUTCFullYear(), yb = new Date(x1).getUTCFullYear();
    const every = Math.max(1, Math.ceil((yb - ya + 1) / Math.max(2, Math.floor((W - PAD.l) / 48))));
    for (let y = ya; y <= yb + 1; y += every) {
      const x = Date.UTC(y, 0, 1);
      if (x < x0 - 1 || x > x1 + 1) continue;
      el("line", { x1: sx(x), x2: sx(x), y1: H - PAD.b, y2: H - PAD.b + 4, stroke: "var(--parent, #94a3b8)" }, svg);
      el("text", { x: sx(x), y: H - 6, "text-anchor": "middle", fill: "var(--muted, #64748b)" }, svg).textContent = y;
    }
    const dotPts = [];
    for (const s of series) {
      if (s.band?.length) {
        const top = s.band.map(([x, , hi]) => `${sx(x)},${sy(hi)}`), bot = s.band.map(([x, lo]) => `${sx(x)},${sy(lo)}`).reverse();
        el("polygon", { points: [...top, ...bot].join(" "), fill: s.color, opacity: 0.14 }, svg);
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
      const r = s.dots ? 3.5 : (pts.length <= 24 ? 2.6 : 0);
      if (r) for (const p of pts) {
        const [x, y] = p;
        const faint = s.faintFrom != null && x > s.faintFrom;
        el("circle", { cx: sx(x), cy: sy(y), r, fill: faint ? "var(--panel, #fff)" : s.color, stroke: s.color,
          "stroke-width": faint ? 1.5 : 0, opacity: s.dots ? 0.78 : 1 }, svg);
        if (s.dots) dotPts.push({ s, p, cx: sx(x), cy: sy(y) });
      }
    }
    // 滑過／點擊：指標靠近散點（14px 內）時優先顯示該筆散點詳情；否則吸附最近的折線時間點
    const guide = el("line", { y1: PAD.t, y2: H - PAD.b, stroke: "var(--parent, #94a3b8)", "stroke-dasharray": "2 2", visibility: "hidden" }, svg);
    const ring = el("circle", { r: 6, fill: "none", stroke: "#ff8f00", "stroke-width": 2, visibility: "hidden" }, svg);
    const tip = tipBox(host);
    const lineSeries = series.filter(s => !s.dots);
    const allX = [...new Set(lineSeries.flatMap(s => s.points.map(p => p[0])))].sort((a, b) => a - b);
    const leave = () => {
      tip.style.display = "none";
      guide.setAttribute("visibility", "hidden");
      ring.setAttribute("visibility", "hidden");
    };
    const move = ev => {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      const px = (ev.clientX - rect.left) * (W / rect.width);
      const py = (ev.clientY - rect.top) * (H / rect.height);
      // 先看是否靠近任一散點
      let bestDot = null, bestD = allX.length ? 14 : 28;
      for (const d of dotPts) {
        const dist = Math.hypot(d.cx - px, d.cy - py);
        if (dist < bestD) { bestD = dist; bestDot = d; }
      }
      if (bestDot) {
        const { s, p, cx, cy } = bestDot;
        guide.setAttribute("x1", cx); guide.setAttribute("x2", cx); guide.setAttribute("visibility", "visible");
        ring.setAttribute("cx", cx); ring.setAttribute("cy", cy); ring.setAttribute("visibility", "visible");
        if (opts.dotFmt) {
          tip.innerHTML = opts.dotFmt(p, s);
        } else {
          const dstr = new Date(p[0]).toISOString().slice(0, 10);
          tip.innerHTML = `<b>${dstr}</b><br><span style="color:${s.color}">●</span> ${escHtml(s.name || "")}：${(opts.tipFmt || opts.yFmt || String)(p[1], p)}`;
        }
        placeTip(tip, rect, cx / W * rect.width, cy / H * rect.height);
        return;
      }
      ring.setAttribute("visibility", "hidden");
      if (!allX.length) { leave(); return; }
      const x = allX.reduce((a, b) => Math.abs(sx(b) - px) < Math.abs(sx(a) - px) ? b : a);
      const rows = lineSeries.filter(s => s.name).map(s => {
        const p = s.points.find(q => q[0] === x);
        return p ? `<span style="color:${s.color}">●</span> ${escHtml(s.name)}：${(opts.tipFmt || opts.yFmt || String)(p[1], p)}` : null;
      }).filter(Boolean);
      if (!rows.length) { leave(); return; }
      const gx = sx(x);
      guide.setAttribute("x1", gx); guide.setAttribute("x2", gx); guide.setAttribute("visibility", "visible");
      tip.innerHTML = `<b>${(opts.xFmt || (v => new Date(v).getUTCFullYear()))(x)}</b><br>${rows.join("<br>")}`;
      placeTip(tip, rect, gx / W * rect.width, py / H * rect.height);
    };
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerdown", move);
    svg.addEventListener("pointerleave", leave);
    host._dismiss = target => { if (!host.contains(target)) leave(); };
    activeDismiss.add(host._dismiss);
  };
  draw();
  watchWidth(host, draw);
}

// 時間軸的直條圖（成交量）。bars: [[x, n, faint]]；opts: { color, xFmt, height }
// 改在整個 SVG 上依 X 座標吸附最近的長條，低成交量或季資料細條也能輕鬆指到。
export function barChart(host, bars, opts = {}) {
  wireDismiss();
  if (host._dismiss) { activeDismiss.delete(host._dismiss); host._dismiss = null; }
  const draw = () => {
    if (host._dismiss) { activeDismiss.delete(host._dismiss); host._dismiss = null; }
    host.style.position = "relative";
    host.querySelectorAll("svg").forEach(s => s.remove());
    if (!bars.length) return;
    const W = Math.max(host.clientWidth, 240), H = opts.height || 110;
    const yt = niceTicks(0, Math.max(...bars.map(b => b[1])), 2);
    const top = yt[yt.length - 1];
    const bw = (W - PAD.l - PAD.r) / bars.length;
    const sy = y => PAD.t + (1 - y / top) * (H - PAD.t - PAD.b);
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img", "aria-label": opts.label || "",
      style: "display:block;overflow:visible;font:11px -apple-system,sans-serif;touch-action:pan-y" }, host);
    for (const v of yt) {
      el("line", { x1: PAD.l, x2: W - PAD.r, y1: sy(v), y2: sy(v), stroke: "var(--line, #e2e8f0)" }, svg);
      el("text", { x: PAD.l - 6, y: sy(v) + 4, "text-anchor": "end", fill: "var(--muted, #64748b)" }, svg).textContent = v.toLocaleString();
    }
    const hiBand = el("rect", { y: PAD.t, height: H - PAD.t - PAD.b, fill: "var(--accent, #2563eb)", opacity: 0.1, visibility: "hidden" }, svg);
    let lastYear = null, lastLabel = -1e9;
    bars.forEach(([x, n, faint], i) => {
      el("rect", { x: PAD.l + i * bw + bw * 0.15, width: Math.max(bw * 0.7, 1), y: sy(n), height: Math.max(sy(0) - sy(n), n > 0 ? 1 : 0),
        fill: faint ? "var(--panel, #fff)" : (opts.color || "#94a3b8"), stroke: faint ? (opts.color || "#94a3b8") : null, rx: 1.5 }, svg);
      const y = new Date(x).getUTCFullYear(), cx = PAD.l + (i + 0.5) * bw;
      if (y !== lastYear && cx - lastLabel >= 34) {  // 年份標籤至少隔 34px，太密就跳過
        el("text", { x: bars.length > 16 ? PAD.l + i * bw : cx, y: H - 6, "text-anchor": bars.length > 16 ? "start" : "middle",
          fill: "var(--muted, #64748b)" }, svg).textContent = y;
        lastLabel = cx;
      }
      lastYear = y;
    });
    const tip = tipBox(host);
    const leave = () => { tip.style.display = "none"; hiBand.setAttribute("visibility", "hidden"); };
    const move = ev => {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      const px = (ev.clientX - rect.left) * (W / rect.width);
      const i = Math.min(Math.max(Math.floor((px - PAD.l) / bw), 0), bars.length - 1);
      const [x, n, faint] = bars[i];
      const bx = PAD.l + i * bw;
      hiBand.setAttribute("x", bx);
      hiBand.setAttribute("width", Math.max(bw, 2));
      hiBand.setAttribute("visibility", "visible");
      tip.innerHTML = `<b>${(opts.xFmt || (v => new Date(v).getUTCFullYear()))(x)}</b><br>${n.toLocaleString()} 筆${faint ? "（資料未齊）" : ""}`;
      placeTip(tip, rect, (bx + bw * 0.5) / W * rect.width, sy(n) / H * rect.height);
    };
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerdown", move);
    svg.addEventListener("pointerleave", leave);
    host._dismiss = target => { if (!host.contains(target)) leave(); };
    activeDismiss.add(host._dismiss);
  };
  draw();
  watchWidth(host, draw);
}

// 清掉圖表（例：切到沒有資料的分類）：停止監看寬度與外部點擊監聽，否則視窗縮放時會把舊圖表畫回來
export function clearChart(host, html = "") {
  host._ro?.disconnect();
  host._ro = null;
  if (host._dismiss) { activeDismiss.delete(host._dismiss); host._dismiss = null; }
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
