"use strict";

/* -- REAL DATA — queried from data/store/ on 2026-08-29 ----------------------
   elements               @ 2026-08-29T07:45:01Z  (288th snapshot since 08-19)
   picks                  @ 2026-08-25T13:12:19Z  (GW1 EO sample, 9,284 entries)
   odds_match_odds        @ 2026-08-27T13:39:14Z  (GW3 fixtures, 13-15 books)
   odds_player_goal_odds  @ 2026-08-27T13:39:20Z  (GW2 fixtures only, 2 books)
   events                 @ 2026-08-28T22:45:01Z  (GW3 deadline, GW2 live average)
   game_config            @ 2026-08-28T23:15:01Z  (scoring, squad rules)
   --------------------------------------------------------------------------- */

/* Track A, the live season. Entry 3434577. Read from the user's own account,
   not from the store — the store samples 9,284 OTHER entries, never this one. */
const SEASON = {
  gw1: {pts:43, avg:50, or:6382778, pct:75},
  gw2: {pts:26, avg:18, or:4461715, pct:40, played:1, xi:11}
};

/* GW2 squad after the one free transfer (Gibbs-White -> Rogers, 27 Aug).
   2 GK / 5 DEF / 5 MID / 3 FWD, £99.6m at current prices, £0.5m banked.
   `eo` and `cap` are REAL — computed from the GW1 picks sample. */
const SQUAD = [
  {n:"Lammens",     t:"MUN", pos:1, price:5.0,  own:15.5, eo:14.4,  cap:0.00},
  {n:"Dubravka",    t:"TOT", pos:1, price:4.0,  own:18.5, eo:0.6,   cap:0.00, bench:1},
  {n:"Gabriel",     t:"ARS", pos:2, price:8.0,  own:28.8, eo:30.8,  cap:1.02},
  {n:"Shaw",        t:"MUN", pos:2, price:4.5,  own:19.4, eo:21.2,  cap:0.00},
  {n:"Van Hecke",   t:"TOT", pos:2, price:5.0,  own:9.5,  eo:9.1,   cap:0.00, watch:true},
  {n:"Diop",        t:"IPS", pos:2, price:4.0,  own:16.9, eo:9.7,   cap:0.00},
  {n:"van Ewijk",   t:"COV", pos:2, price:4.0,  own:13.0, eo:4.3,   cap:0.00, bench:2},
  {n:"B.Fernandes", t:"MUN", pos:3, price:12.0, own:47.4, eo:70.8,  cap:17.61, vice:true},
  {n:"Rogers",      t:"CHE", pos:3, price:7.5,  own:26.1, eo:23.4,  cap:0.08, isNew:true},
  {n:"Szoboszlai",  t:"LIV", pos:3, price:7.0,  own:43.4, eo:41.9,  cap:0.45},
  {n:"Tzolis",      t:"ARS", pos:3, price:6.5,  own:26.1, eo:26.2,  cap:1.02, out:true},
  {n:"Hughes",      t:"CRY", pos:3, price:4.5,  own:10.7, eo:0.5,   cap:0.00, bench:3},
  {n:"Haaland",     t:"MCI", pos:4, price:15.5, own:68.3, eo:121.7, cap:50.61, capt:true},
  {n:"João Pedro",  t:"CHE", pos:4, price:7.6,  own:68.4, eo:68.4,  cap:6.19},
  {n:"Kusi-Asare",  t:"FUL", pos:4, price:4.5,  own:6.9,  eo:0.3,   cap:0.13, bench:4}
];

/* Ownership series: every 9th of 288 snapshots, 19 Aug -> 29 Aug. REAL. */
const OWN_SERIES = {
  "Tzolis":        [19.7,21.3,21.9,23.1,24.9,25.2,25.2,25.2,25.2,25.2,25.3,25.3,25.4,25.4,25.7,25.7,25.7,25.7,25.8,25.8,25.9,25.9,25.9,26.0,26.0,26.0,26.1,26.1,26.1,26.1],
  "Ødegaard":      [6.6,7.1,7.2,7.6,8.0,8.2,8.3,8.3,8.4,8.6,8.8,9.0,9.4,9.7,10.4,10.4,10.5,10.6,10.6,10.9,11.2,11.2,11.3,11.4,11.4,11.5,11.6,11.7,11.8,11.8],
  "Gakpo":         [3.6,3.6,3.6,3.5,3.5,3.5,3.5,3.5,3.5,3.6,3.6,3.6,3.7,4.0,4.7,4.8,4.9,5.0,5.0,5.4,5.7,5.8,5.9,6.0,6.0,6.1,6.3,6.5,6.6,6.6],
  "Foden":         [4.4,4.4,4.3,4.3,4.3,4.4,4.4,4.4,4.4,4.4,4.4,4.4,4.4,4.4,4.3,4.3,4.3,4.3,4.2,4.2,4.2,4.1,4.1,4.1,4.1,4.1,4.1,4.0,4.1,4.1],
  "M.Sangaré":     [2.9,3.1,3.1,3.3,3.4,3.3,3.3,3.3,3.3,3.2,3.4,3.8,4.2,4.6,5.7,5.9,6.0,6.2,6.3,7.0,7.5,7.7,7.8,8.0,8.1,8.3,8.6,9.0,9.2,9.3],
  "Ndiaye":        [15.8,15.9,15.9,16.0,16.1,16.0,15.9,15.9,15.9,15.8,15.9,15.9,16.0,16.0,16.1,16.1,16.1,16.1,16.1,16.1,16.0,16.0,16.0,16.0,16.0,16.1,16.1,16.1,16.0,16.0],
  "Stach":         [1.0,1.0,1.0,0.9,0.9,0.9,0.9,0.9,0.9,0.9,1.0,1.0,1.2,1.2,1.4,1.4,1.5,1.5,1.6,1.8,2.0,2.1,2.1,2.2,2.2,2.3,2.4,2.5,2.6,2.7],
  "Dewsbury-Hall": [3.4,3.4,3.3,3.3,3.2,3.2,3.2,3.2,3.2,3.2,3.4,3.5,3.8,3.9,4.2,4.2,4.2,4.2,4.3,4.3,4.4,4.5,4.5,4.5,4.6,4.6,4.7,4.7,4.7,4.7],
  "Tavernier":     [1.7,1.6,1.6,1.6,1.6,1.6,1.6,1.6,1.6,1.6,1.5,1.5,1.7,1.8,2.0,2.0,2.0,2.0,2.0,2.1,2.2,2.2,2.2,2.3,2.3,2.3,2.4,2.5,2.5,2.5],
  "Elanga":        [0.7,0.7,0.7,0.7,0.7,0.7,0.7,0.7,0.8,0.8,0.8,0.8,0.9,1.1,1.4,1.4,1.5,1.5,1.5,1.7,1.8,1.8,1.9,1.9,1.9,2.0,2.1,2.1,2.2,2.2],
  "Hinshelwood":   [0.4,0.4,0.4,0.4,0.4,0.4,0.4,0.4,0.4,0.4,0.4,0.4,0.8,1.0,1.5,1.5,1.6,1.7,1.8,2.1,2.5,2.5,2.6,2.6,2.6,2.6,2.4,2.3,2.2,2.2]
};

/* GW3 fixtures with de-vigged 1X2, median inverse price across books. REAL —
   odds_match_odds captured 27 Aug 21:39 local. The FPL fixture table itself
   still holds matchweek 1 only, so this is the odds feed standing in for an
   ingest that has not happened. Kickoffs shown in local time (GMT+8). */
const GW3_FIXTURES = [
  {h:"IPS", a:"LIV", ko:"Sat 5 Sep 03:00", ph:18.5, pd:22.5, pa:59.0, books:15},
  {h:"NEW", a:"BOU", ko:"Sat 5 Sep 19:30", ph:40.7, pd:26.4, pa:33.0, books:14},
  {h:"BHA", a:"LEE", ko:"Sat 5 Sep 22:00", ph:52.9, pd:25.8, pa:21.3, books:14},
  {h:"BRE", a:"SUN", ko:"Sat 5 Sep 22:00", ph:57.1, pd:24.4, pa:18.5, books:13},
  {h:"FUL", a:"CRY", ko:"Sat 5 Sep 22:00", ph:40.4, pd:28.1, pa:31.5, books:14},
  {h:"MCI", a:"COV", ko:"Sat 5 Sep 22:00", ph:78.3, pd:13.9, pa:7.8,  books:14},
  {h:"NFO", a:"TOT", ko:"Sat 5 Sep 22:00", ph:34.4, pd:28.5, pa:37.1, books:14},
  {h:"HUL", a:"AVL", ko:"Sun 6 Sep 00:30", ph:23.6, pd:25.7, pa:50.7, books:14},
  {h:"EVE", a:"MUN", ko:"Sun 6 Sep 21:00", ph:31.9, pd:28.0, pa:40.2, books:14},
  {h:"ARS", a:"CHE", ko:"Sun 6 Sep 23:30", ph:57.0, pd:24.1, pa:18.8, books:13}
];

/* Top of the GW1 effective-ownership table. REAL — 9,284 sampled entries,
   139,260 picks. `eo` is sum(multiplier)/entries; `cap` is captaincy share.
   This is the dataset that cannot be backfilled. */
const EO_TOP = [
  {n:"Haaland",       own:70.3, eo:121.7, cap:50.61},
  {n:"B.Fernandes",   own:52.8, eo:70.8,  cap:17.61},
  {n:"João Pedro",    own:64.1, eo:68.4,  cap:6.19},
  {n:"Szoboszlai",    own:42.8, eo:41.9,  cap:0.45},
  {n:"Mbeumo",        own:39.0, eo:41.7,  cap:2.93},
  {n:"Calafiori",     own:40.9, eo:40.9,  cap:0.27},
  {n:"Raya",          own:38.2, eo:38.9,  cap:0.40},
  {n:"Gabriel",       own:29.6, eo:30.8,  cap:1.02},
  {n:"Calvert-Lewin", own:32.2, eo:27.1,  cap:0.66},
  {n:"Isak",          own:17.0, eo:20.2,  cap:3.60}
];

/* --------------------------------------------------------------------------
   CANDIDATES — midfielders inside £7.0m (Tzolis sale £6.5m + £0.5m bank).

   real:  everything up to and including `gw3`
   fake:  everything under `f:` — pStart, eMin, pG, pA, pDC, xpts, p10, p90,
          pHaul, rankEv, horizon, verdict, note
   Keep that boundary exact.
   -------------------------------------------------------------------------- */
const CANDS = [
  { n:"Tzolis", t:"ARS", price:6.5, own:26.1, ownD:6.4, outgoing:true,
    eo:26.2, cap:1.02,
    gw1:{pts:6, min:75, xg:0.19, xa:0.14, bps:30, dc:6}, pens:null,
    tIn:16478, tOut:8715, pchg:94.3,
    gw3:{opp:"CHE", ha:"H", win:57.0},
    f:{pStart:0.78, eMin:66, pG:0.19, pA:0.13, pDC:0.24, xpts:4.1, p10:1, p90:10, pHaul:0.10,
       rankEv:0, horizon:0} },

  { n:"Ødegaard", t:"ARS", price:6.6, own:11.8, ownD:5.2,
    eo:8.9, cap:0.56,
    gw1:{pts:11, min:75, xg:0.21, xa:0.10, bps:41, dc:8}, pens:3,
    tIn:8978, tOut:4501, pchg:5.9,
    gw3:{opp:"CHE", ha:"H", win:57.0},
    f:{pStart:0.86, eMin:74, pG:0.17, pA:0.22, pDC:0.29, xpts:4.7, p10:1, p90:12, pHaul:0.13,
       rankEv:0.11, horizon:2.4,
       verdict:"MARGINAL", note:"Same fixture as the player he replaces, +0.6 projected points, and 17.3pp less effective ownership — which in a rank-aware objective is an argument for the move, not against it. The catch is that the EO figure is a GW1 sample being read into a GW3 decision, and his ownership line has moved 6.6 to 11.8 since it was taken. One sample is not a trajectory."} },

  { n:"Gakpo", t:"LIV", price:7.0, own:6.6, ownD:3.0,
    eo:3.4, cap:0.04,
    gw1:{pts:12, min:90, xg:0.30, xa:0.15, bps:33, dc:12}, pens:3,
    tIn:9059, tOut:4466, pchg:82.0,
    gw3:{opp:"IPS", ha:"A", win:59.0},
    f:{pStart:0.71, eMin:62, pG:0.22, pA:0.16, pDC:0.33, xpts:4.4, p10:0, p90:12, pHaul:0.12,
       rankEv:0.04, horizon:1.1,
       verdict:"HOLD", note:"Best raw fixture in the band at 59% away, 12 defensive contributions from a full 90, third in the penalty order. He also spends the entire budget and sits at 82.0% on the price gauge, so the move is likely to cost more tomorrow. Rotation is the whole question and it is the question no served model can answer."} },

  { n:"Foden", t:"MCI", price:7.0, own:4.1, ownD:-0.3,
    eo:4.0, cap:0.08,
    gw1:{pts:1, min:81, xg:0.12, xa:0.40, bps:15, dc:6}, pens:null,
    tIn:9965, tOut:5662, pchg:-25.0,
    gw3:{opp:"COV", ha:"H", win:78.3},
    f:{pStart:0.64, eMin:55, pG:0.21, pA:0.20, pDC:0.21, xpts:4.2, p10:0, p90:13, pHaul:0.14,
       rankEv:-0.03, horizon:0.4,
       verdict:"HOLD", note:"City at home to a promoted side is the shortest price in the gameweek — 78.3% across 14 books, the only GW3 fixture above 60%. 0.40 xA in 81 minutes says he was involved. The 0.64 start probability is the entire objection, and it is the one number on this row that a built model could answer and nothing serves."} },

  { n:"Ndiaye", t:"EVE", price:6.0, own:16.0, ownD:0.2,
    eo:14.7, cap:0.04,
    gw1:{pts:9, min:90, xg:0.24, xa:0.32, bps:36, dc:12}, pens:1,
    tIn:6822, tOut:12490, pchg:28.3,
    gw3:{opp:"MUN", ha:"H", win:31.9},
    f:{pStart:0.88, eMin:79, pG:0.16, pA:0.15, pDC:0.38, xpts:4.0, p10:0, p90:11, pHaul:0.10,
       rankEv:-0.02, horizon:-0.6,
       verdict:"HOLD", note:"First on penalties, a full 90, 12 defensive contributions, and £1.0m cheaper. Everton at 31.9% at home to United is the worst fixture on this list, and 12,490 managers left him this gameweek against 6,822 in — the crowd is moving the other way, and the crowd can read the same fixture."} },

  { n:"M.Sangaré", t:"BRE", price:5.6, own:9.4, ownD:6.5,
    eo:1.9, cap:0.01,
    gw1:{pts:14, min:75, xg:0.05, xa:0.33, bps:41, dc:13}, pens:null,
    tIn:26838, tOut:3157, pchg:30.8,
    gw3:{opp:"SUN", ha:"H", win:57.1},
    f:{pStart:0.89, eMin:80, pG:0.04, pA:0.09, pDC:0.61, xpts:3.9, p10:1, p90:9, pHaul:0.06,
       rankEv:0.02, horizon:0.9,
       verdict:"HOLD", note:"13 defensive contributions and 14 points from a £5.6m midfielder, in the second-shortest home price of the gameweek, freeing £0.9m. The DC model is built and gated and its MID/FWD threshold of 12 is pinned by observation — this row is the clearest case on the screen of a model existing and nothing being able to put a question to it."} },

  { n:"Stach", t:"LEE", price:6.0, own:2.7, ownD:1.7,
    eo:0.6, cap:0.00,
    gw1:{pts:13, min:90, xg:0.10, xa:0.05, bps:37, dc:16}, pens:null,
    tIn:5941, tOut:1887, pchg:31.8,
    gw3:{opp:"BHA", ha:"A", win:21.3},
    f:{pStart:0.91, eMin:84, pG:0.05, pA:0.06, pDC:0.66, xpts:3.6, p10:1, p90:8, pHaul:0.04,
       rankEv:-0.01, horizon:-0.3,
       verdict:"HOLD", note:"16 defensive contributions is the highest figure in the sample and clears the pinned threshold of 12 by a distance. Very little of it converts without attacking return, and Leeds away at 21.3% is the second-worst fixture here. This is a floor, not a ceiling."} },

  { n:"Dewsbury-Hall", t:"EVE", price:6.5, own:4.7, ownD:1.3,
    eo:2.9, cap:0.01,
    gw1:{pts:11, min:90, xg:0.12, xa:0.17, bps:40, dc:8}, pens:null,
    tIn:5063, tOut:3251, pchg:50.1,
    gw3:{opp:"MUN", ha:"H", win:31.9},
    f:{pStart:0.83, eMin:74, pG:0.09, pA:0.14, pDC:0.31, xpts:3.5, p10:0, p90:10, pHaul:0.07,
       rankEv:-0.04, horizon:-1.2,
       verdict:"HOLD", note:"40 BPS from a full 90 is a bonus-model signal, and the bonus model is built — 223 folds, log-loss 0.1881, its PMF derived per fixture rather than per player because bonus is a rank-within-fixture phenomenon. It is also the same Everton fixture as Ndiaye, £0.5m dearer, without the penalties."} },

  { n:"Tavernier", t:"BOU", price:6.0, own:2.6, ownD:0.9,
    eo:0.9, cap:0.00,
    gw1:{pts:10, min:90, xg:0.40, xa:0.08, bps:32, dc:8}, pens:3,
    tIn:3635, tOut:1482, pchg:36.2,
    gw3:{opp:"NEW", ha:"A", win:33.0},
    f:{pStart:0.80, eMin:72, pG:0.13, pA:0.09, pDC:0.27, xpts:3.3, p10:0, p90:10, pHaul:0.07,
       rankEv:-0.06, horizon:-1.8,
       verdict:"HOLD", note:"0.40 xG is the highest open-play figure in the band and he is third on penalties. Away at Newcastle for 33.0% is what that costs. At 0.9% effective ownership the downside is completely unhedged, which is precisely the trade the rank-aware objective is meant to price and currently cannot."} },

  { n:"Elanga", t:"NEW", price:6.0, own:2.2, ownD:1.5,
    eo:0.6, cap:0.00,
    gw1:{pts:9, min:75, xg:0.22, xa:0.01, bps:31, dc:2}, pens:null,
    tIn:6443, tOut:1164, pchg:42.4,
    gw3:{opp:"BOU", ha:"H", win:40.7},
    f:{pStart:0.74, eMin:64, pG:0.14, pA:0.12, pDC:0.13, xpts:3.2, p10:0, p90:10, pHaul:0.07,
       rankEv:-0.07, horizon:-2.1,
       verdict:"HOLD", note:"2 defensive contributions is the lowest here, so nothing floors this pick: it is attacking return or nothing, from 75 minutes and a 40.7% home fixture. It also fails the interim P(start) ≥ 0.75 rule by 0.01, which is the kind of margin an interim rule should never be asked to arbitrate."} },

  { n:"Hinshelwood", t:"BHA", price:6.0, own:2.2, ownD:1.8,
    eo:0.3, cap:0.01, flag:"Unspecified injury · 25% chance of playing",
    gw1:{pts:16, min:63, xg:1.41, xa:0.02, bps:54, dc:3}, pens:null,
    tIn:1064, tOut:11634, pchg:43.8,
    gw3:{opp:"LEE", ha:"H", win:52.9},
    f:{pStart:0.31, eMin:24, pG:0.11, pA:0.05, pDC:0.14, xpts:1.5, p10:0, p90:8, pHaul:0.05,
       rankEv:-0.22, horizon:-5.7,
       verdict:"BLOCK", note:"The best single gameweek in the sample — 16 points, 1.41 xG, 54 BPS from 63 minutes — and FPL now flags him at 25% to play. 11,634 managers out this week against 1,064 in. The flag is observed; the 0.31 start probability underneath it is not, and there is no news layer to say what the injury actually is."} }
];
