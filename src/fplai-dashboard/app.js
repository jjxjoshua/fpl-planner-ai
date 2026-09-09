"use strict";

/* -- helpers -------------------------------------------------------------- */
const POSNAME = {1:"Goalkeepers", 2:"Defenders", 3:"Midfielders", 4:"Forwards"};
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const f1 = n => n.toFixed(1);
const commas = n => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");

function sparkline(series, w, h){
  const min = Math.min(...series), max = Math.max(...series);
  const span = (max - min) || 1;
  const pts = series.map((v,i) => [
    (i/(series.length-1))*(w-2)+1,
    h-1 - ((v-min)/span)*(h-2)
  ]);
  const d = pts.map((p,i) => (i?"L":"M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const rising = series[series.length-1] >= series[0];
  const col = rising ? "var(--live)" : "var(--gap)";
  const last = pts[pts.length-1];
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">
    <path d="${d}" fill="none" stroke="${col}" stroke-width="1.3" stroke-linejoin="round"/>
    <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="1.9" fill="${col}"/>
  </svg>`;
}

/* Player-gameweek PMF for a MIDFIELDER. The shape is genuine — a convolution
   over the real scoring constants read from game_config: goal 5, assist 3,
   defensive contribution 2, clean sheet 1, appearance 1/2, bonus 0-3. Every
   RATE fed into it is fabricated, because nothing serves a per-player GW3
   feature row. The GW2 player-goal market was never captured for GW3
   fixtures, so unlike the 24 Aug draw of this screen not even the goal rate
   is observed here. */
function poisson(lam, k){ let p = Math.exp(-lam); for(let i=1;i<=k;i++) p *= lam/i; return p; }

function pmf(c){
  const N = 21;
  const out = new Array(N).fill(0);
  const lam = -Math.log(1 - Math.min(c.f.pG, 0.92));   /* goals   */
  const la  = -Math.log(1 - Math.min(c.f.pA, 0.92));   /* assists */
  const pDC = c.f.pDC;
  const pCS = 0.10 + c.gw3.win / 900;                  /* MID clean sheet = 1 pt */
  const pStart = c.f.pStart, pApp = Math.min(pStart + 0.06, 0.99);
  out[0] += 1 - pApp;
  const branches = [[2, pStart*0.92], [1, pApp - pStart*0.92]];
  for(const [mp, w] of branches){
    for(let g=0; g<=4; g++){
      const pg = poisson(lam, g);
      for(let a=0; a<=3; a++){
        const pa = poisson(la, a);
        for(const [dc, pd] of [[2, pDC], [0, 1-pDC]]){
          for(const [cs, pc] of [[1, pCS], [0, 1-pCS]]){
            const ret = g + a;
            const bd = ret >= 2 ? [[3,.45],[2,.25],[1,.15],[0,.15]]
                     : ret === 1 ? [[3,.12],[2,.16],[1,.22],[0,.50]]
                                 : [[1,.06],[0,.94]];
            for(const [b, pb] of bd){
              const pts = mp + 5*g + 3*a + dc + cs + b;
              out[Math.min(pts, N-1)] += w * pg * pa * pd * pc * pb;
            }
          }
        }
      }
    }
  }
  return out;
}

function pmfChart(a, b, labelA, labelB){
  const A = pmf(a), B = pmf(b);
  const N = 17, W = 300, H = 96, pad = 14;
  const maxY = Math.max(...A.slice(0,N), ...B.slice(0,N));
  const bw = (W - pad) / N;
  let bars = "";
  for(let i=0;i<N;i++){
    const ha = (A[i]/maxY)*(H-pad-8), hb = (B[i]/maxY)*(H-pad-8);
    const x = pad + i*bw;
    bars += `<rect x="${(x+1).toFixed(1)}" y="${(H-pad-ha).toFixed(1)}" width="${(bw*0.44).toFixed(1)}" height="${ha.toFixed(1)}" fill="var(--gap)" opacity="0.75"/>`;
    bars += `<rect x="${(x+1+bw*0.48).toFixed(1)}" y="${(H-pad-hb).toFixed(1)}" width="${(bw*0.44).toFixed(1)}" height="${hb.toFixed(1)}" fill="var(--live)" opacity="0.85"/>`;
  }
  let ticks = "";
  for(const i of [0,4,8,12,16]){
    ticks += `<text x="${(pad + i*bw + bw*0.5).toFixed(1)}" y="${H-3}" font-family="IBM Plex Mono, monospace" font-size="8" fill="var(--ink-3)" text-anchor="middle">${i===16?"16+":i}</text>`;
  }
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Illustrative points distribution comparing ${esc(labelA)} and ${esc(labelB)}">
    <line x1="${pad}" y1="${H-pad}" x2="${W}" y2="${H-pad}" stroke="var(--rule-2)" stroke-width="1"/>
    ${bars}${ticks}
  </svg>`;
}

/* -- render: season strip ------------------------------------------------- */
function renderSeason(){
  const g1 = SEASON.gw1, g2 = SEASON.gw2;
  document.getElementById("season").innerHTML = `
    <div class="sitem">
      <span class="sk">GW1 settled</span>
      <span class="sv"><b class="dn">${g1.pts}</b> vs ${g1.avg} avg</span>
      <span class="sm">OR ${commas(g1.or)} &#183; ${g1.pct}th pct</span>
    </div>
    <span class="sarrow">&#8594;</span>
    <div class="sitem live">
      <span class="sk">GW2 in progress</span>
      <span class="sv"><b class="up">${g2.pts}</b> vs ${g2.avg} avg</span>
      <span class="sm">OR ${commas(g2.or)} &#183; ${g2.pct}th pct &#183; provisional</span>
    </div>
    <div class="sitem">
      <span class="sk">Played</span>
      <span class="sv">${g2.played} <small>of ${g2.xi} XI</small></span>
      <span class="sm">all ${g2.pts} points are the captain</span>
    </div>`;
}

/* -- render: GW3 fixture strip -------------------------------------------- */
function renderFixtures(){
  const rows = GW3_FIXTURES.map(f => {
    const strong = Math.max(f.ph, f.pa);
    const fav = f.ph >= f.pa ? f.h : f.a;
    return `<div class="fx" title="${f.books} books, de-vigged">
      <span class="fk">${f.h} <i>v</i> ${f.a}</span>
      <span class="fbar">
        <i class="h" style="width:${f.ph}%"></i><i class="d" style="width:${f.pd}%"></i><i class="a" style="width:${f.pa}%"></i>
      </span>
      <span class="fv ${strong >= 55 ? "hot" : ""}">${fav} ${strong.toFixed(0)}%</span>
    </div>`;
  }).join("");
  document.getElementById("fixtures").innerHTML = rows;
}

/* -- render: EO leaderboard ----------------------------------------------- */
function renderEo(){
  const max = EO_TOP[0].eo;
  document.getElementById("eoBody").innerHTML = EO_TOP.map(p => `
    <div class="eor">
      <span class="en">${esc(p.n)}</span>
      <span class="ebar"><i style="width:${(p.eo/max*100).toFixed(1)}%"></i><u style="width:${(p.own/max*100).toFixed(1)}%"></u></span>
      <span class="ev">${p.eo.toFixed(1)}</span>
      <span class="ec">${p.cap.toFixed(1)}%</span>
    </div>`).join("");
}

/* -- render: squad rail --------------------------------------------------- */
function renderRail(sel){
  const starters = SQUAD.filter(p => !p.bench);
  const bench = SQUAD.filter(p => p.bench).sort((a,b) => a.bench - b.bench);
  const row = p => {
    const cls = ["pl"];
    if (p.out) cls.push("out");
    if (p.watch) cls.push("flag");
    let badge = "";
    if (p.capt) badge = `<span class="cb c">C</span>`;
    else if (p.vice) badge = `<span class="cb v">V</span>`;
    else if (p.isNew) badge = `<span class="cb n">NEW</span>`;
    return `<div class="${cls.join(" ")}">
      <span class="nm">${esc(p.n)}${badge}</span>
      <span class="tm">${p.t}</span>
      <span class="pr">${f1(p.price)}</span>
      <span class="eoc" title="GW1 effective ownership, observed">${p.eo.toFixed(1)}</span>
    </div>`;
  };
  let html = "";
  for(const pos of [1,2,3,4]){
    const group = starters.filter(p => p.pos === pos);
    if(!group.length) continue;
    html += `<div class="poslabel">${POSNAME[pos]}</div>` + group.map(row).join("");
    if(pos === 3 && sel){
      html += `<div class="pl inn">
        <span class="nm">${esc(sel.n)}</span>
        <span class="tm">${sel.t}</span>
        <span class="pr">${f1(sel.price)}</span>
        <span class="eoc">${sel.eo.toFixed(1)}</span>
      </div>`;
    }
  }
  html += `<div class="bench"><div class="poslabel">Bench</div>` + bench.map(row).join("") + `</div>`;
  document.getElementById("railBody").innerHTML = html;
  document.getElementById("railNote").textContent = sel ? "1 change staged" : "15 players";
}

/* -- render: matrix ------------------------------------------------------- */
function renderMatrix(selName){
  const body = document.getElementById("matrixBody");
  body.innerHTML = CANDS.map(c => {
    const isOut = !!c.outgoing;
    const active = c.n === selName;
    const cls = ["row"];
    if (isOut) cls.push("outrow");
    if (active) cls.push("active");
    const series = OWN_SERIES[c.n] || [];
    const dCls = c.ownD >= 0 ? "delta up" : "delta dn";
    const dTxt = (c.ownD >= 0 ? "+" : "") + f1(c.ownD);
    const tag = isOut ? `<span class="tag outg">Out</span>`
              : active ? `<span class="tag sel">In</span>` : "";
    const flag = c.flag ? `<span class="tag warnt" title="${esc(c.flag)}">Flag</span>` : "";
    const fxCls = c.gw3.win >= 55 ? "hot" : c.gw3.win <= 25 ? "cold" : "";
    return `<tr class="${cls.join(" ")}" data-name="${esc(c.n)}" tabindex="0" role="button"
             aria-pressed="${active}" aria-label="Preview replacing Tzolis with ${esc(c.n)}">
      <td><div class="who"><span class="n">${esc(c.n)}</span><span class="t">${c.t}</span>${tag}${flag}</div></td>
      <td><div class="cell">${f1(c.price)}</div></td>
      <td><div class="cell">${f1(c.own)}</div></td>
      <td><div class="cell" style="display:flex;align-items:center;gap:7px;justify-content:flex-end">
        ${sparkline(series, 34, 15)}<span class="${dCls}">${dTxt}</span></div></td>
      <td><div class="cell"><b>${c.eo.toFixed(1)}</b>
        <span style="color:var(--ink-3);font-size:10px"> ${c.cap.toFixed(1)}%</span></div></td>
      <td><div class="cell"><b>${c.gw1.pts}</b>
        <span style="color:var(--ink-3);font-size:10.5px"> ${c.gw1.min}&#8242;</span></div></td>
      <td><div class="cell">${(c.gw1.xg + c.gw1.xa).toFixed(2)}
        <span style="color:var(--ink-3);font-size:10.5px"> ${c.gw1.dc}</span></div></td>
      <td><div class="cell ${fxCls}"><span style="color:var(--ink-3)">${c.gw3.ha === "H" ? "v" : "@"}</span>${c.gw3.opp} ${c.gw3.win.toFixed(0)}%</div></td>
      <td><div class="cell fab m"><span class="v">${c.f.pStart.toFixed(2)}</span></div></td>
      <td><div class="cell fab m"><span class="v">${(c.f.pG*100).toFixed(0)}/${(c.f.pA*100).toFixed(0)}</span></div></td>
      <td><div class="cell fab g"><span class="v">${c.f.xpts.toFixed(1)}</span></div></td>
      <td><div class="cell fab g"><span class="v">${(c.f.pHaul*100).toFixed(0)}%</span></div></td>
    </tr>`;
  }).join("");

  body.querySelectorAll("tr.row").forEach(tr => {
    const pick = () => {
      const nm = tr.getAttribute("data-name");
      if (nm === "Tzolis") return;
      select(nm);
    };
    tr.addEventListener("click", pick);
    tr.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
    });
  });
}

/* -- render: detail ------------------------------------------------------- */
function renderDetail(c){
  const T = CANDS[0];
  const freed = T.price - c.price;
  const bankAfter = 0.5 + freed;
  const maxEo = Math.max(T.eo, c.eo, 1);
  const eoLadder = [
    ["Tzolis", T.eo, T.cap, "a"],
    [c.n,      c.eo, c.cap, "b"]
  ].map(([nm, v, cap, k]) => `
    <div class="lrow">
      <span class="lk">${esc(nm.length > 11 ? nm.slice(0,10) + "…" : nm)}</span>
      <span class="bar"><i class="${k}" style="width:${(v/maxEo*100).toFixed(1)}%"></i></span>
      <span class="lv">${v.toFixed(1)}</span>
    </div>`).join("");

  const fx = GW3_FIXTURES.find(f => f.h === c.t || f.a === c.t);
  const home = fx.h === c.t;
  const mine = home ? fx.ph : fx.pa;

  const vClass = c.f.verdict === "BLOCK" ? "g" : c.f.verdict === "MARGINAL" ? "o" : "m";
  const vTitle = c.f.verdict === "BLOCK" ? "Do not transfer"
               : c.f.verdict === "MARGINAL" ? "Defensible, unproven" : "Keep Tzolis";

  document.getElementById("detail").innerHTML = `
    <div class="swap">
      <span class="eyebrow">Staged transfer &#183; 1 of 1 free</span>
      <div class="swapline"><span class="o">Tzolis</span><span class="arrow">&#8594;</span><span class="i">${esc(c.n)}</span></div>
      <div class="swapmeta">
        <span>Frees <b>&#163;${f1(freed)}m</b></span>
        <span>Bank after <b>&#163;${f1(bankAfter)}m</b></span>
        <span>Hit <b>0 pts</b></span>
        <span>Team limit <b>${c.t === "ARS" ? "2 ARS, ok" : "ok"}</b></span>
      </div>
    </div>

    <div class="block">
      <span class="eyebrow">Effective ownership, GW1 <span class="pill o">Observed</span></span>
      <div class="ladder">${eoLadder}</div>
      <div class="ledger" style="margin-top:2px">
        <div class="lr"><span class="a">Captaincy share</span><span class="b">${T.cap.toFixed(2)}% &#8594; ${c.cap.toFixed(2)}%</span></div>
        <div class="lr"><span class="a">EO delta if swapped</span><span class="b" style="color:${c.eo < T.eo ? "var(--live)" : "var(--gap)"}">${(c.eo - T.eo >= 0 ? "+" : "")}${(c.eo - T.eo).toFixed(1)} pp</span></div>
        <div class="lr"><span class="a">Sample</span><span class="b">9,284 entries &#183; 139,260 picks</span></div>
        <div class="lr"><span class="a">Taken</span><span class="b" style="color:var(--warn)">GW1, 25 Aug &#183; one gameweek stale</span></div>
      </div>
    </div>

    <div class="block">
      <span class="eyebrow">GW3 fixture, de-vigged <span class="pill o">Observed</span></span>
      <div class="fxbig">
        <div class="fxline"><b>${fx.h}</b> <i>v</i> <b>${fx.a}</b><span>${fx.ko} local</span></div>
        <span class="fbar big">
          <i class="h" style="width:${fx.ph}%"></i><i class="d" style="width:${fx.pd}%"></i><i class="a" style="width:${fx.pa}%"></i>
        </span>
        <div class="fxkey"><span>${fx.h} ${fx.ph.toFixed(1)}%</span><span>Draw ${fx.pd.toFixed(1)}%</span><span>${fx.a} ${fx.pa.toFixed(1)}%</span></div>
      </div>
      <div class="ledger">
        <div class="lr"><span class="a">${esc(c.n)}&#8217;s side</span><span class="b">${mine.toFixed(1)}% to win</span></div>
        <div class="lr"><span class="a">Books &#183; captured</span><span class="b">${fx.books} &#183; 27 Aug 21:39 local</span></div>
        <div class="lr"><span class="a">Anytime-goal market, GW3</span><span class="b" style="color:var(--gap)">not captured</span></div>
      </div>
    </div>

    <div class="block">
      <span class="eyebrow">Points distribution, GW3 <span class="pill g">Illustrative</span></span>
      <div class="chart">
        ${pmfChart(T, c, "Tzolis", c.n)}
        <div class="chartkey">
          <span><i style="background:var(--gap)"></i>Tzolis</span>
          <span><i style="background:var(--live)"></i>${esc(c.n)}</span>
        </div>
      </div>
      <div class="ledger">
        <div class="lr"><span class="a">Mean <b>xPts</b></span><span class="b">${T.f.xpts.toFixed(1)} &#8594; ${c.f.xpts.toFixed(1)}</span></div>
        <div class="lr"><span class="a">10th&#8211;90th percentile</span><span class="b">${T.f.p10}&#8211;${T.f.p90} &#8594; ${c.f.p10}&#8211;${c.f.p90}</span></div>
        <div class="lr"><span class="a">P(10+ points)</span><span class="b">${(T.f.pHaul*100).toFixed(0)}% &#8594; ${(c.f.pHaul*100).toFixed(0)}%</span></div>
        <div class="lr"><span class="a">P(start) &#183; E[minutes]</span><span class="b">${c.f.pStart.toFixed(2)} &#183; ${c.f.eMin}</span></div>
        <div class="lr"><span class="a">P(goal) &#183; P(assist) &#183; P(DC)</span><span class="b">${(c.f.pG*100).toFixed(0)}% &#183; ${(c.f.pA*100).toFixed(0)}% &#183; ${(c.f.pDC*100).toFixed(0)}%</span></div>
      </div>
    </div>

    <div class="eo-alert">
      <b>GW2 effective ownership is not collected yet</b>
      <p>The GW1 sample is banked &#8212; 9,284 entries, and the numbers above are real. The GW2 sample is due <b>Tue 1 Sep, evening local</b>, once the gameweek settles. Every gameweek missed is a permanent hole: FPL reissues entry IDs annually and captaincy has no archive.</p>
    </div>

    <div class="verdict ${vClass === "o" ? "swap" : ""}">
      <div class="vh">
        <span class="pill ${vClass}">${c.f.verdict}</span>
        <b>${vTitle}</b>
      </div>
      <p>${esc(c.f.note)}</p>
      <div class="ledger" style="margin-top:3px">
        <div class="lr"><span class="a">Rank-EV, top-10% probability</span><span class="b">${(c.f.rankEv >= 0 ? "+" : "")}${c.f.rankEv.toFixed(2)} pp</span></div>
        <div class="lr"><span class="a">5-gameweek horizon, net</span><span class="b">${(c.f.horizon >= 0 ? "+" : "")}${c.f.horizon.toFixed(1)} pts</span></div>
        <div class="lr"><span class="a">GW4&#8211;GW7 fixtures</span><span class="b" style="color:var(--gap)">not ingested</span></div>
      </div>
    </div>

    <div class="block">
      <span class="eyebrow">Provenance</span>
      <div class="ledger">
        <div class="lr"><span class="a"><b>elements</b> &#8212; price, ownership, GW1 line</span><span class="b">2026-08-29T07:45:01Z</span></div>
        <div class="lr"><span class="a"><b>picks</b> &#8212; effective ownership, captaincy</span><span class="b">2026-08-25T13:12:19Z</span></div>
        <div class="lr"><span class="a"><b>odds_match_odds</b> &#8212; GW3 1X2</span><span class="b">2026-08-27T13:39:14Z</span></div>
        <div class="lr"><span class="a"><b>game_config</b> &#8212; scoring, squad rules</span><span class="b">2026-08-28T23:15:01Z</span></div>
        <div class="lr"><span class="a"><b>events</b> &#8212; GW3 deadline</span><span class="b">2026-08-28T22:45:01Z</span></div>
        <div class="lr"><span class="a" style="color:var(--model)">P(start) &#183; P(goal) &#183; P(assist) &#183; P(DC)</span><span class="b" style="color:var(--model)">model built, not served</span></div>
        <div class="lr"><span class="a" style="color:var(--gap)">xPts &#183; P(10+) &#183; rank-EV &#183; verdict</span><span class="b" style="color:var(--gap)">fabricated</span></div>
      </div>
    </div>
  `;
}

/* -- wire ----------------------------------------------------------------- */
function select(name){
  const c = CANDS.find(x => x.n === name);
  if (!c) return;
  renderMatrix(name);
  renderRail(c);
  renderDetail(c);
}
renderSeason();
renderFixtures();
renderEo();
select("Ødegaard");
