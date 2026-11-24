"use strict";
(async () => {
  const { el, api, say } = Hub, out = document.getElementById("out");
  try {
    for (const w of (await api("GET", "api/watchers")).watchers) {
      const box = el("div", {});
      let loaded = false;
      const d = el("details", { class: "panel" },
        el("summary", {}, w.name, el("span", { class: "note" }, `${w.status} · ${w.risk} risk · ${w.actions_today} actions today`)),
        el("p", { class: "note" }, w.description || ""),
        el("p", { class: "note" }, `Runs: ${w.triggers.join("; ")}`),
        el("p", { class: "note" }, `AI step's tools: ${w.tools.join(", ") || "none"}`),
        el("p", { class: "note" }, "Allowed without asking by buttons in Telegram: " + (w.grants.map((g) => {
          const to = Object.values(g.route).filter(Boolean).join(" / ");
          return to ? `${g.op} (${to})` : g.op;
        }).join(", ") || "nothing")),
        el("p", { class: "note" }, "What starts it, its files and its messages to you aren't controlled here."), box);
      d.addEventListener("toggle", () => {
        if (!d.open || loaded) return;
        loaded = true;
        abilities(w.scope, box).catch((e) => { loaded = false; say(e.message, true); });
      });
      out.append(d);
    }
  } catch (e) { say(e.message, true); }
})();
