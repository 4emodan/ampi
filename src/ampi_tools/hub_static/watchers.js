"use strict";
(() => {
  const { api, el, show, say, when, row, button, told } = Hub;
  const load = () => show(async (out) => {
    const open = new Set([...out.querySelectorAll("details[open]")].map((d) => d.dataset.w));
    const [{ watchers }, { topics }] = await Promise.all([api("GET", "api/watchers"), api("GET", "api/topics")]);
    const places = topics.filter((t) => t.private);
    out.replaceChildren();
    if (!watchers.length) out.append(el("p", { class: "empty" }, "None yet. Ask Ampi for one."));
    for (const w of watchers) {
      const to = el("select", { "aria-label": "Its messages go to" }, el("option", { value: "" }, "Main chat"),
        ...places.filter((t) => !t.scope.endsWith("/0") && t.scope.split(":")[1].split("/")[0] === w.scope.split(":")[1].split("/")[0])
          .map((t) => { const o = el("option", { value: t.scope }, t.title || t.where); o.selected = t.scope === w.deliver; return o; }));
      to.addEventListener("change", async () => {
        try { const r = await api("POST", "api/deliver", { scope: w.scope, topic: to.value }); await load(); told(r, "Back to the main chat."); } catch (e) { to.value = w.deliver; say(e.message, true); }
      });
      const l = w.limits;
      out.append(el("details", { class: "panel", "data-w": w.scope, ...(open.has(w.scope) ? { open: "" } : {}) },
        el("summary", {}, el("span", { class: `dot ${w.status}` }), w.name, el("span", { class: "note" }, `${w.status} · ${w.risk} risk · ${w.actions_today} today`)),
        el("p", { class: "note" }, w.description || ""),
        row("Runs", w.triggers.join("; ") || "when you ask"),
        row("Last acted", when(w.last)),
        row("Its AI step's tools", w.tools.join(", ") || "none"),
        row("A day at most", `${l.runs_per_day} runs, ${l.llm_calls_per_day} AI calls, ${l.actions_per_day} actions`),
        row("Its messages, reports and requests go to", w.deliver_waiting ? "a change waits for your Approve" : null, to),
        row("Allowed with buttons in Telegram", w.grants.map((g) => { const x = Object.values(g.route).filter(Boolean).join(" / "); return x ? `${g.op} (${x})` : g.op; }).join(", ") || "nothing"),
        el("div", { class: "actions" },
          w.status === "paused"
            ? w.resume_waiting ? el("span", { class: "tag" }, "waits for your Approve") : button("Turn on", async () => { const r = await api("POST", "api/resume", { scopes: [w.scope] }); await load(); told(r); }, "primary")
            : button("Pause", async () => { await api("POST", "api/pause", { scopes: [w.scope] }); await load(); say(`${w.name} is paused.`); }),
          el("a", { class: "btn", href: `abilities#s=${encodeURIComponent(w.scope)}` }, "What it may do"),
          el("a", { class: "btn", href: `activity#w=${encodeURIComponent(w.name)}` }, "What it did"))));
    }
  });
  load();
})();
