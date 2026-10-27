(() => {
  "use strict";
  const SVGNS = "http://www.w3.org/2000/svg";
  const charts = [];
  let lastLoad = 0;

  // ---- tiny DOM helpers (text only: nothing from the store is ever parsed as HTML) -------------------------
  function put(e, kids) {
    for (const k of kids.flat(3)) {
      if (k == null || k === false) continue;
      e.append(k.nodeType ? k : document.createTextNode(String(k)));
    }
  }
  function attrs(e, a) {
    for (const [k, v] of Object.entries(a || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? "" : v);
    }
  }
  const h = (tag, a, ...kids) => { const e = document.createElement(tag); attrs(e, a); put(e, kids); return e; };
  const sv = (tag, a, ...kids) => { const e = document.createElementNS(SVGNS, tag); attrs(e, a); put(e, kids); return e; };

  // ---- formatting ------------------------------------------------------------------------------------------
  const int = (n) => Number(n).toLocaleString("en-US");
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // A date-only string ("2026-08-10", a bucket start) is a calendar day: read it with UTC getters so it never
  // shifts to the day before in a timezone west of UTC. A full timestamp is shown in the viewer's own day.
  function day(iso, withYear) {
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const utc = /^\d{4}-\d{2}-\d{2}$/.test(iso);
    const date = utc ? d.getUTCDate() : d.getDate(), mon = utc ? d.getUTCMonth() : d.getMonth();
    const yr = utc ? d.getUTCFullYear() : d.getFullYear();
    const s = date + " " + MONTHS[mon];
    return withYear || yr !== new Date().getFullYear() ? s + " " + yr : s;
  }
  // Whole percent that reads as 100 or 0 only when it is exactly all or none.
  const pct = (x) => (x >= 1 ? 100 : x <= 0 ? 0 : Math.min(99, Math.max(1, Math.round(x * 100))));
  function ymd(s) { const p = s.split("-"); return MONTHS[Number(p[1]) - 1] + " " + p[0]; }
  function ago(sec) {
    if (sec == null) return "never";
    if (sec < 90) return "just now";
    if (sec < 5400) return Math.round(sec / 60) + " min ago";
    if (sec < 129600) return Math.round(sec / 3600) + " h ago";
    return Math.round(sec / 86400) + " days ago";
  }
  const plural = (n, w) => int(n) + " " + w + (n === 1 ? "" : "s");

  // ---- charts ----------------------------------------------------------------------------------------------
  function niceMax(max) {
    const steps = [0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000];
    const step = steps.find((s) => max / s <= 4) || steps[steps.length - 1];
    return { step, top: Math.max(step, Math.ceil(max / step) * step) };
  }
  function holder(draw) {
    const box = h("div", { class: "chartbox" });
    charts.push({ box, draw });
    return box;
  }
  function drawAll() {
    for (const c of charts) {
      const w = Math.floor(c.box.clientWidth) || 560;
      c.box.replaceChildren(c.draw(Math.max(260, w)));
    }
  }

  function lineChart(series, unit) {
    return (W) => {
      const H = 236, ml = 40, mr = 14, mt = 14, mb = 32, iw = W - ml - mr, ih = H - mt - mb;
      const maxv = Math.max(...series.map((s) => s.mean), 0.5);
      const { step, top } = niceMax(maxv);
      const t = series.map((s) => new Date(s.start).getTime());
      const t0 = t[0], t1 = t[t.length - 1];
      const X = (i) => ml + (t1 === t0 ? iw / 2 : ((t[i] - t0) / (t1 - t0)) * iw);
      const Y = (v) => mt + ih - (v / top) * ih;
      const svg = sv("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img",
        "aria-label": "Edits per draft over time" });
      for (let v = 0; v <= top + 1e-9; v += step) {
        svg.append(sv("line", { class: v === 0 ? "axis" : "grid", x1: ml, x2: W - mr, y1: Y(v), y2: Y(v) }));
        svg.append(sv("text", { x: ml - 8, y: Y(v) + 4, "text-anchor": "end" }, +v.toFixed(1)));
      }
      svg.append(sv("text", { class: "ytitle", x: ml, y: 8 }, "edits per draft"));
      const nT = W < 420 ? 3 : 5;
      for (let k = 0; k < nT; k++) {
        const at = t0 + ((t1 - t0) * k) / (nT - 1);
        const x = t1 === t0 ? ml + iw / 2 : ml + (iw * k) / (nT - 1);
        svg.append(sv("text", { x, y: H - 8, "text-anchor": k === 0 ? "start" : k === nT - 1 ? "end" : "middle" },
          day(new Date(at).toISOString().slice(0, 10))));
      }
      svg.append(sv("polyline", { class: "ln", points: series.map((s, i) => `${X(i)},${Y(s.mean)}`).join(" ") }));
      series.forEach((s, i) => {
        const last = i === series.length - 1;
        svg.append(sv("circle", { class: "pt" + (last ? " last" : ""), cx: X(i), cy: Y(s.mean), r: last ? 5 : 3.6 },
          sv("title", null, `${unit === "week" ? "Week of " : ""}${day(s.start)}: ${s.mean} edits per draft (${plural(s.n, "draft")})`)));
      });
      return svg;
    };
  }

  function barChart(series) {
    return (W) => {
      const H = 190, ml = 34, mr = 6, mt = 14, mb = 26, iw = W - ml - mr, ih = H - mt - mb;
      const { step, top } = niceMax(Math.max(...series.map((s) => s.n), 1));
      const Y = (v) => mt + ih - (v / top) * ih;
      const svg = sv("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Texts read per month" });
      for (let v = 0; v <= top + 1e-9; v += step) {
        svg.append(sv("line", { class: v === 0 ? "axis" : "grid", x1: ml, x2: W - mr, y1: Y(v), y2: Y(v) }));
        svg.append(sv("text", { x: ml - 7, y: Y(v) + 4, "text-anchor": "end" }, int(v)));
      }
      const slot = iw / series.length, bw = Math.min(30, slot * 0.62);
      series.forEach((s, i) => {
        const x = ml + slot * i + (slot - bw) / 2;
        svg.append(sv("rect", { class: "bar", x, y: Y(s.n), width: bw, height: Math.max(0, Y(0) - Y(s.n)), rx: 3 },
          sv("title", null, `${ymd(s.month)}: ${plural(s.n, "text")}`)));
        if (W >= 520 || i % 2 === series.length % 2) {
          svg.append(sv("text", { x: x + bw / 2, y: H - 7, "text-anchor": "middle" }, MONTHS[Number(s.month.slice(5)) - 1]));
        }
      });
      return svg;
    };
  }

  const unavailable = (need) => h("p", { class: "unavail" }, h("b", null, "Not enough data yet. "), need ? "Needs " + need + "." : "");

  // ---- sections --------------------------------------------------------------------------------------------
  function hero(d) {
    const s = d.sources;
    const el = h("div", { class: "hero fade" }, h("h1", null, "your voice"));
    if (d.stage === "empty") {
      el.append(h("p", { class: "lede" }, d.next_step));
      return el;
    }
    el.append(h("p", { class: "lede" }, "Learned from ", h("b", null, plural(d.total_units, "text")), " across ",
      h("b", null, plural(s.with_texts, "source")), ", in ", h("b", null, plural(d.cards.cards.length, "register")),
      " so far."));
    if (s.kinds.length) {
      el.append(h("div", { class: "strip" }, s.kinds.map((k) =>
        h("div", { class: "kind" }, h("span", { class: "num" }, int(k.units)), h("span", { class: "l" }, k.label)))));
    }
    const line = h("div", { class: "syncline" }, h("span", null, "Last sync ", ago(s.last_sync_age_s)));
    if (d.pending && d.pending.label) line.append(h("span", null, plural(d.pending.label, "text"), " waiting for a label"));
    el.append(line);
    if (s.sources.length) {
      const rows = s.sources.map((r) => h("tr", null,
        h("td", null, r.name), h("td", null, r.kind), h("td", { class: "num" }, int(r.units)),
        h("td", null, r.enabled ? (r.last_success ? ago((Date.now() - new Date(r.last_success)) / 1000) : "not yet") : "off"),
        h("td", { class: r.last_error ? "err" : "" }, r.last_error || "")));
      el.append(h("details", { class: "srcs" }, h("summary", null, "Sync state by source"),
        h("table", null, h("thead", null, h("tr", null, ["Source", "Kind", "Texts", "Last sync", "Last error"].map((x) => h("th", null, x)))),
          h("tbody", null, rows))));
    }
    return el;
  }

  const STATUS = {
    fresh: ["fresh", "The card matches your recent writing."],
    upgrade: ["upgrade available", "New text or edits arrived since the agent wrote this card; ask your agent to refresh it."],
    stale: ["stale", "Your recent writing has drifted from this card."],
    building: ["building", "Not enough messages for a full card yet."],
  };

  function habit(x) {
    const fill = h("div", { class: "fill" });
    fill.dataset.w = String(Math.max(2, Math.round(x.share * 100)));
    const el = h("div", { class: "habit" },
      h("div", { class: "hrow" }, h("b", null, x.label), h("span", { class: "c num" }, int(x.count) + " of " + int(x.of) + " · " + pct(x.share) + "%")),
      h("div", { class: "track" }, fill));
    if (x.example) {
      el.append(h("div", { class: "ex" }, h("i", null, "“" + x.example.text + "”"),
        h("small", null, x.example.kind + " · " + ymd(x.example.month))));
    }
    return el;
  }

  function card(c) {
    const [label, tip] = STATUS[c.status] || STATUS.fresh;
    const el = h("article", { class: "card fade" },
      h("div", { class: "chead" }, h("div", null, h("h3", null, c.register), h("p", { class: "note" }, c.note)),
        h("span", { class: "chip " + c.status, title: tip }, label)),
      h("p", { class: "from" }, "Measured in " + int(c.scanned) + (c.scanned < c.units ? " of your " + int(c.units) : "") + " messages · " +
        (c.card_source === "agent" ? "agent-written card" : "stats card") + (c.updated ? " · updated " + day(c.updated) : "")));
    if (c.habits.length) c.habits.forEach((x) => el.append(habit(x)));
    else el.append(h("p", { class: "waiting" }, "No habit stands out yet; the card will sharpen as more of your writing comes in."));
    if (c.text) el.append(h("details", { class: "cardtext" }, h("summary", null, "Card text your agent receives"), h("pre", null, c.text)));
    return el;
  }

  function cardsSection(d) {
    const sec = h("section", { id: "cards" }, h("div", { class: "shead" }, h("h2", null, "Voice cards"),
      h("p", null, "One per kind of writing. Every habit is counted in your own messages, with a real example.")));
    const c = d.cards;
    if (c.cards.length) sec.append(h("div", { class: "cards" }, c.cards.map(card)));
    else sec.append(h("div", { class: "panel empty" }, "No card yet. " + (d.next_step || "")));
    if (c.waiting.length) {
      sec.append(h("p", { class: "waiting" }, "Still collecting: " + c.waiting.map((w) => `${w.register} (${w.units} of ${w.need} messages)`).join(", ") + "."));
    }
    return sec;
  }

  function diffSide(title, ops, keep, mark) {
    const body = h("div", null);
    ops.forEach((o, i) => {
      if (o.t !== "eq" && o.t !== keep) return;
      if (i) body.append(" ");
      body.append(o.t === "eq" ? o.s : h("span", { class: mark }, o.s));
    });
    return h("div", { class: "side" }, h("h4", null, title), body);
  }

  function pairView(p, open) {
    return h("details", { class: "pair", open: open }, h("summary", null, "Draft vs. what you sent"),
      h("div", { class: "diff" }, diffSide("Agent draft", p.ops, "del", "del"), diffSide("What you sent", p.ops, "ins", "ins")));
  }

  function learnedSection(d) {
    const L = d.learned;
    const sec = h("section", { id: "learned" }, h("div", { class: "shead" }, h("h2", null, "Learned from your edits"),
      h("p", null, "When you change what an agent drafted, mannerism pairs the two and turns repeated changes into rules.")));
    if (L.rules.length) {
      sec.append(h("div", { class: "rules" }, L.rules.map((r, i) => {
        const el = h("article", { class: "panel rule fade" }, h("p", { class: "t" }, r.text),
          h("div", { class: "meta" }, h("span", { class: "reg" }, r.register), h("span", null, "learned " + day(r.learned)),
            r.edits ? h("span", null, "from " + plural(r.edits, "edit")) : null));
        if (r.pair) el.append(pairView(r.pair, i === 0));
        return el;
      })));
      if (L.hidden) sec.append(h("p", { class: "waiting" }, plural(L.hidden, "rule") + " hidden because later edits contradicted them."));
    } else if (L.recent_pairs.length) {
      sec.append(h("p", { class: "waiting" }, plural(L.pending_edits, "edit") + " captured so far. " + L.need));
      sec.append(h("div", { class: "rules" }, L.recent_pairs.map((p, i) => h("article", { class: "panel rule" },
        h("div", { class: "meta" }, h("span", { class: "reg" }, p.register), h("span", null, day(p.ts))), pairView(p, i === 0)))));
    } else {
      sec.append(h("div", { class: "panel empty" }, h("b", null, "No edits captured yet. "),
        "Ask your agent to draft something in your name, then change it. The draft and your version appear here, and repeated changes become rules."));
    }
    return sec;
  }

  function tile(t) {
    const ok = t.available;
    const el = h("div", { class: "tile fade" + (ok ? "" : " no") }, h("div", { class: "l" }, t.label));
    if (!ok) {
      el.append(h("span", { class: "v" }, "Not enough data yet"));
      if (t.need) el.append(h("div", { class: "need" }, "Needs " + t.need));
      return el;
    }
    const v = h("span", { class: "v num" });
    if (t.format === "pct") v.append(String(t.value), h("small", null, "%"));
    else if (t.format === "months") v.append(String(t.value), h("small", null, t.value === 1 ? "month" : "months"));
    else if (t.format === "num1") v.append(t.value.toFixed(1));
    else if (t.format === "words") v.append(String(Math.round(t.value)), h("small", null, "words"));
    else v.append(int(t.value));
    el.append(v);
    if (t.sub) el.append(h("div", { class: "s" }, t.sub));
    return el;
  }

  function panel(title, sub, cls) {
    return h("div", { class: "panel fade " + (cls || "") }, h("h3", null, title), sub ? h("p", { class: "sub" }, sub) : null);
  }

  function insightsSection(d) {
    const I = d.insights;
    const sec = h("section", { id: "insights" }, h("div", { class: "shead" }, h("h2", null, "Insights"),
      h("p", null, "Computed from what is stored. Where there is too little data, it says so.")));
    sec.append(h("div", { class: "tiles" }, I.tiles.map(tile)));
    const grid = h("div", { class: "charts" });

    const e = panel("Edits per draft", "How many separate passages you change in an agent's draft before sending, over drafts matched to what you sent. Lower means the draft already sounds like you.", "wide");
    const t = I.edits_per_draft;
    if (t.ok) {
      e.append(h("div", { class: "big" }, h("span", { class: "num" }, t.first.toFixed(1)), h("span", { class: "arrow" }, "→"),
        h("span", { class: "num now" }, t.latest.toFixed(1)),
        h("span", { class: "cap" }, `first ${t.window} of ${plural(t.pairs, "draft")} vs. latest ${t.window}`)));
      e.append(holder(lineChart(t.series, t.bucket)));
    } else e.append(unavailable(t.need));
    grid.append(e);

    const w = panel("Sent as written", "Drafts you sent at least 90% the same. Only drafts matched to something you wrote count; a draft you rewrote completely, or never sent, is left out.");
    const sw = I.written;
    if (sw.ok) {
      const share = pct(sw.share);
      w.append(h("div", { class: "big" }, h("span", { class: "num" }, share + "%"),
        h("span", { class: "cap" }, `${sw.as_written} of ${plural(sw.pairs, "matched draft")}` +
          (sw.recent_share != null ? ` · last 30 days: ${pct(sw.recent_share)}%` : ""))));
      const bar = h("div", { class: "stack", role: "img", "aria-label": share + " percent sent as written" }, h("i", { class: "a" }));
      bar.firstChild.style.width = sw.share * 100 + "%";
      w.append(bar, h("div", { class: "legend" }, h("span", null, "as written " + sw.as_written), h("span", { class: "e" }, "edited " + sw.edited)));
    } else w.append(unavailable(sw.need));
    grid.append(w);

    const tl = panel("AI tells caught", "Phrases the lint flagged in drafts, left out of your own writing.");
    const tc = I.tells;
    if (tc.ok) {
      tl.querySelector(".sub").textContent = `${tc.with_tells} of ${plural(tc.drafts, "logged draft")} had at least one AI tell` +
        (tc.uncalibrated.length ? `. Little of your ${tc.uncalibrated.join(", ")} writing is stored yet, so there only the most telling phrases count.`
          : " that you do not use yourself.") +
        (tc.skipped_long ? ` ${plural(tc.skipped_long, "longer draft")} not checked.` : "");
      if (tc.top.length) {
        const max = Math.max(...tc.top.map((x) => x.drafts));
        tl.append(h("div", { class: "hbars" }, tc.top.map((x) => {
          const f = h("div", { class: "fl" });
          f.style.width = Math.max(4, (x.drafts / max) * 100) + "%";
          return h("div", { class: "hb" }, h("span", null, x.label), h("div", { class: "tr" }, f), h("span", { class: "n num" }, x.drafts));
        })));
      } else tl.append(h("p", { class: "unavail" }, "No tells found in your logged drafts."));
    } else tl.append(unavailable(tc.need));
    grid.append(tl);

    const wd = panel("Words per message", "Typical range by register: the bar spans the middle half, the tick marks the median.");
    const wr = I.words;
    if (wr.ok) {
      const max = Math.max(...wr.registers.map((r) => r.p75), 1);
      wd.append(h("div", { class: "hbars" }, wr.registers.map((r) => {
        const tr = h("div", { class: "tr" });
        const band = h("div", { class: "fl soft" }), tick = h("div", { class: "md" });
        band.style.left = (r.p25 / max) * 100 + "%";
        band.style.width = Math.max(1.5, ((r.p75 - r.p25) / max) * 100) + "%";
        tick.style.left = "calc(" + (r.median / max) * 100 + "% - 1.5px)";
        tr.append(band, tick);
        return h("div", { class: "hb" }, h("span", { class: "name" }, r.register), tr,
          h("span", { class: "n num" }, Math.round(r.median) + " words"));
      })));
      wd.append(h("p", { class: "sub" }, "Median per register; 0 to " + Math.round(max) + " words."));
    } else wd.append(unavailable(wr.need));
    grid.append(wd);

    const mo = panel("Texts read, by month", "How much of your writing mannerism has to learn from, by the month you wrote it.");
    if (I.months.ok) mo.append(holder(barChart(I.months.series)));
    else mo.append(unavailable(I.months.need));
    grid.append(mo);
    sec.append(grid);
    return sec;
  }


  // ---- "Is this you?" (the only part of the page that writes; two POSTs, a one-time CSRF token in a header) --
  // mode: "ok" (this tab holds the CSRF token), "readonly" (the one-time token went to another tab), "ended"
  // (the server no longer knows this session). Without "ok" the section shows the groups but no buttons.
  const rv = { data: null, error: null, confirm: null, busy: new Set(), notice: null, showAll: false, csrf: null,
    mode: "ok", seq: 0, seen: new Set(), placeFirst: null };
  const reviewEl = h("section", { id: "review", "aria-labelledby": "review-h" });
  const SOURCE_NAMES = { claude_code: "Claude Code", codex: "Codex", chatgpt: "ChatGPT", opencode: "OpenCode",
    qwen_code: "Qwen Code", git: "Git commits", gh: "GitHub", x_archive: "X archive", mbox: "Mail import" };
  const niceSource = (s) => SOURCE_NAMES[s] || String(s).replace(/_/g, " ");
  const groupTitle = (g) => (g.kind === "template" ? "A message that repeats" : niceSource(g.source) + (g.project ? " · " + g.project : ""));
  const ENDED = "This page session has ended. Run `mannerism ui` again to open a new one.";
  const TOKEN_KEY = "mannerism-csrf";
  const store = {  // sessionStorage can be blocked: every access is wrapped, and the memory copy in rv.csrf is the fallback
    get() { try { return sessionStorage.getItem(TOKEN_KEY) || null; } catch (e) { return null; } },
    set(v) { try { sessionStorage.setItem(TOKEN_KEY, v); } catch (e) { /* memory only */ } },
    clear() { try { sessionStorage.removeItem(TOKEN_KEY); } catch (e) { /* ignore */ } },
  };

  function kv(label, value) {
    return value ? h("div", { class: "kv" }, h("dt", null, label), h("dd", null, value)) : null;
  }

  function breakdown(g) {
    const b = g.breakdown;
    if (!b || !b.by_origin.length) return null;
    return h("div", { class: "crumbs" }, h("p", null, "These texts are stored under:"),
      h("ul", null, b.by_origin.map((o) => h("li", null, niceSource(o.source) + (o.project ? " · " + o.project : "") + ": " + plural(o.units, "text"))),
        b.more ? h("li", null, "and " + plural(b.more, "other place")) : null));
  }

  function groupCard(g) {
    const busy = rv.busy.has(g.group_id), asking = rv.confirm === g.group_id;
    const kept = g.decision === "keep", can = rv.mode === "ok";
    const fresh = !rv.seen.has(g.group_id);
    rv.seen.add(g.group_id);
    const el = h("article", { class: "panel grp" + (fresh ? " fade" : "") + (g.held ? " held" : ""), "data-group": g.group_id },
      h("div", { class: "chead" },
        h("div", null, h("h3", null, groupTitle(g)),
          h("p", { class: "note" }, g.kind === "template"
            ? "The same opening in " + plural(g.sessions, "session") + ". Often a script, a relay or a template."
            : plural(g.units, "text") + " · " + plural(g.sessions, "session"))),
        g.held ? h("span", { class: "chip fresh" }, int(g.held) + " held")
          : kept ? h("span", { class: "chip" }, "marked as you") : null));
    if (g.kind === "origin") {
      el.append(h("dl", { class: "kvs" }, kv("Account", g.account), kv("Project", g.project), kv("Session", g.session_kind)));
    } else {
      el.append(h("p", { class: "from" }, plural(g.units, "text") + " in total"));
    }
    const range = g.first && g.last ? day(g.first, true) + (day(g.first, true) === day(g.last, true) ? "" : " – " + day(g.last, true)) : "";
    if (range) el.append(h("p", { class: "from" }, range));
    if (g.held) {
      el.append(h("p", { class: "heldnote" }, int(g.held) + (g.held === 1 ? " text looks" : " texts look") +
        " typed by a machine, so " + (g.held === 1 ? "it is" : "they are") + " held back from your voice until you decide."));
    }
    if (g.samples.length) {
      el.append(h("ul", { class: "samples", "aria-label": "Examples (private details are masked)" },
        g.samples.map((t) => h("li", null, t))));
    }
    if (g.mixed) {  // older texts from several places: deleting them as one group could remove a person's own writing
      el.append(h("p", { class: "mixednote" }, "These are older texts from several places, so they cannot be removed as one group. " +
        "Run `mannerism review rescan` to sort them."));
    }
    if (!can) return el;
    if (asking) {
      const no = h("button", { type: "button", class: "btn", "data-act": "cancel", onclick: () => { rv.confirm = null; paintReview(); } }, "Keep them");
      const yes = h("button", { type: "button", class: "btn solid", "data-act": "confirm-not-me", onclick: () => act("not-me", g) },
        "Delete " + plural(g.units, "message"));
      el.append(h("div", { class: "confirm", role: "alertdialog", "aria-label": "Confirm deleting this group" },
        h("p", null, h("b", null, "Delete " + plural(g.units, "stored message") + " from this group? "),
          "mannerism also removes what it learned from them and skips this group from now on. This cannot be undone."),
        breakdown(g), h("div", { class: "acts" }, yes, no)));
      queueMicrotask(() => no.focus());
    } else {
      const acts = h("div", { class: "acts" });
      if (!g.mixed) {
        acts.append(h("button", { type: "button", class: "btn", "data-act": "not-me", disabled: busy, onclick: () => { rv.confirm = g.group_id; paintReview(); } }, "Not me"));
      }
      if (g.held || !kept) {
        acts.append(h("button", { type: "button", class: "btn primary", "data-act": "its-me", disabled: busy, onclick: () => act("its-me", g) },
          busy ? "Working…" : "It's me"));
      }
      el.append(acts);
    }
    return el;
  }

  function modeNote() {
    if (rv.mode === "readonly") {
      return h("p", { class: "notice", role: "status" }, "This tab can look but not change anything: the page's one-time key went to another tab. " +
        "To answer here, close the other tabs, restart `mannerism ui` and open the link it prints.");
    }
    if (rv.mode === "ended") return h("p", { class: "notice bad", role: "status" }, ENDED);
    return null;
  }

  function paintReview() {
    const head = h("div", { class: "shead" }, h("h2", { id: "review-h" }, "Is this you?"),
      h("p", null, "mannerism learns only from what you wrote. These groups of texts may include scripts, relays or other tools that typed for you."));
    const kids = [head, modeNote()];
    if (rv.notice) kids.push(h("p", { class: "notice" + (rv.notice.error ? " bad" : ""), role: "status", "data-notice": "1" }, rv.notice.text));
    if (rv.error) {
      kids.push(h("div", { class: "panel empty" }, rv.error));
    } else if (rv.data) {
      const all = rv.data.groups, held = all.filter((g) => g.held > 0), rest = all.filter((g) => !(g.held > 0));
      const shown = rv.showAll ? all : held;
      if (!shown.length) {
        kids.push(h("div", { class: "panel empty" }, h("b", null, "Nothing needs a second look. "),
          "Everything mannerism has stored looks like it came from you" + (rv.data.decisions.length ? ", and your earlier answers are remembered" : "") + "."));
      } else {
        kids.push(h("div", { class: "grid-grp" }, shown.map(groupCard)));
      }
      if (rest.length) {
        kids.push(h("p", { class: "more" }, h("button", { type: "button", class: "btn", "data-act": "toggle-all", onclick: () => { rv.showAll = !rv.showAll; paintReview(); } },
          rv.showAll ? "Show only the held groups" : (held.length ? "Show the other " : "Look through all ") + plural(rest.length, "group"))));
      }
      if (rv.data.groups_total > all.length) {
        kids.push(h("p", { class: "waiting" }, "Showing the first " + int(all.length) + " of " + int(rv.data.groups_total) + " groups. The rest are small; use `mannerism scan` to list them."));
      }
    } else {
      kids.push(h("p", { class: "waiting" }, "Looking through your stored texts…"));
    }
    reviewEl.replaceChildren(...kids);
  }

  function reveal() {  // the section can be far down a long page: bring the result into view
    const n = reviewEl.querySelector("[data-notice]");
    if (n && n.scrollIntoView) n.scrollIntoView({ block: "nearest" });
  }

  // The CSRF token is handed out once per launch. The first tab to ask gets it and keeps it in sessionStorage; any other
  // tab is refused ("used") and stays read-only.
  let tokenReady = null;
  function getToken() {
    if (!tokenReady) {
      tokenReady = (async () => {
        const saved = store.get();
        if (saved) { rv.csrf = saved; return; }
        try {
          const r = await fetch("/api/csrf", { cache: "no-store", credentials: "same-origin" });
          const d = await r.json().catch(() => ({}));
          if (r.ok && d.ok && d.csrf) { rv.csrf = d.csrf; store.set(d.csrf); }
          else rv.mode = d && d.error === "used" ? "readonly" : "ended";
        } catch (err) { rv.mode = "ended"; }
        if (rv.mode !== "ok") paintReview();
      })();
    }
    return tokenReady;
  }

  async function act(kind, g) {
    const label = groupTitle(g);
    rv.busy.add(g.group_id); rv.confirm = null; rv.notice = null;
    paintReview();
    try {
      await getToken();
      if (!rv.csrf) throw new Error(rv.mode === "readonly" ? "This tab cannot make changes." : ENDED);
      const body = { group_id: g.group_id };
      if (kind === "not-me") body.expected_units = g.units;
      const r = await fetch("/api/review/" + kind, { method: "POST", cache: "no-store", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Mannerism-Csrf": rv.csrf }, body: JSON.stringify(body) });
      if (r.status === 403) { rv.csrf = null; rv.mode = "ended"; store.clear(); throw new Error(ENDED); }
      const d = await r.json().catch(() => ({}));
      if (!d.ok) throw new Error(d.error || "That did not work. Nothing was changed.");
      rv.notice = { text: kind === "not-me"
        ? "Removed " + plural(d.removed, "message") + " from " + label + ". mannerism will skip this group from now on."
        : (d.released ? "Released " + plural(d.released, "held message") + " from " + label + " into your voice." : "Marked " + label + " as you.") +
          " New texts from it will not be held." };
    } catch (err) {
      rv.notice = { error: true, text: err && err.message ? err.message : "Could not reach the local server." };
    }
    rv.busy.delete(g.group_id);
    await loadReview();
    await load({ review: false });
    reveal();
  }

  async function loadReview() {
    const mine = ++rv.seq;  // replies can arrive out of order; only the newest request may paint
    let next;
    try {
      const r = await fetch("/api/review", { cache: "no-store", credentials: "same-origin" });
      const d = await r.json();
      if (r.status === 403) next = { error: ENDED };
      else if (!d.ok) next = { error: "Could not read the groups: " + (d.error || "unknown error") };
      else next = { data: d };
    } catch (err) {
      next = { error: "Could not reach the local server." };
    }
    if (mine !== rv.seq) return;
    if (next.data) { rv.data = next.data; rv.error = null; } else rv.error = next.error;
    paintReview();
  }

  // ---- page ------------------------------------------------------------------------------------------------
  function render(d) {
    charts.length = 0;
    const main = document.getElementById("main");
    document.getElementById("hero").replaceChildren(hero(d));
    if (d.stage === "empty") {
      main.replaceChildren(h("div", { class: "panel empty fade" }, h("b", null, "Start here. "), d.next_step));
      return;
    }
    paintReview();
    // Held texts need an answer, so the question goes first. The place is fixed at the first draw, so the section does
    // not jump to the bottom of the page when the last held group is answered.
    if (rv.placeFirst === null && rv.data) rv.placeFirst = rv.data.held_total > 0;
    const first = !!rv.placeFirst;
    main.replaceChildren(...(first ? [reviewEl] : []), cardsSection(d), learnedSection(d), insightsSection(d), ...(first ? [] : [reviewEl]));
    drawAll();
    requestAnimationFrame(() => document.querySelectorAll(".fill").forEach((f) => { f.style.width = f.dataset.w + "%"; }));
  }

  function fail(msg) {
    document.getElementById("hero").replaceChildren(h("div", { class: "hero" }, h("h1", null, "your voice")));
    document.getElementById("main").replaceChildren(h("p", { class: "loading" }, msg));
  }

  let inflight = false, again = false, againReview = false;
  async function load(opts) {
    const wantReview = !opts || opts.review !== false;
    if (inflight) { again = true; againReview = againReview || wantReview; return; }
    inflight = true;
    try {
      getToken();  // asks for the one-time token at page load, before anything else can
      const reviewing = wantReview ? (rv.notice = null, loadReview()) : null;
      const r = await fetch("/api/overview", { cache: "no-store", credentials: "same-origin" });
      if (r.status === 403) return fail("This page session has ended. Run `mannerism ui` again to open a new one.");
      const d = await r.json();
      if (!d.ok) return fail("mannerism could not read its store: " + (d.error || "unknown error"));
      if (reviewing) await reviewing;
      lastLoad = Date.now();
      render(d);
    } catch (err) {
      fail("Could not reach the local server. Is `mannerism ui` still running?");
    } finally {
      inflight = false;
      if (again) { const review = againReview; again = againReview = false; load({ review }); }
    }
  }

  function theme() {
    const root = document.documentElement;
    try { const t = localStorage.getItem("mannerism-theme"); if (t === "light" || t === "dark") root.dataset.theme = t; } catch (e) { /* storage may be blocked */ }
    document.getElementById("theme").addEventListener("click", () => {
      const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      try { localStorage.setItem("mannerism-theme", root.dataset.theme); } catch (e) { /* ignore */ }
    });
  }

  theme();
  document.getElementById("refresh").addEventListener("click", () => load());
  document.addEventListener("visibilitychange", () => { if (!document.hidden && Date.now() - lastLoad > 30000) load(); });
  let rz = 0;
  addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(drawAll, 120); });
  load();
})();
