"use strict";
(() => {
  const { api, el, when, row, twice, button, told, show, say } = Hub;
  const load = () => show(async (out) => {
    const h = await api("GET", "api/home"), t = h.today;
    out.replaceChildren();
    if (h.stopped) {
      out.append(el("div", { class: "panel" }, row("Everything is stopped", "Watchers are paused and Ampi changes nothing.",
        button("Turn on again", async () => { told(await api("POST", "api/resume", { everything: true })); }, "primary"))));
    }
    out.append(el("h2", {}, "Needs you"));
    const bad = h.watchers.filter((w) => w.status === "failing");
    const need = el("div", { class: "panel" });
    for (const p of h.pending) {
      need.append(row(`#${p.id} · ${p.summary.split("\n").slice(0, 3).join(" ").slice(0, 200)}`, `asked ${when(p.at)}${p.expires ? `, until ${when(p.expires)}` : ""} · answer in Telegram`));
    }
    for (const w of bad) need.append(row(`Watcher ${w.name} keeps failing`, "See its last error in Telegram: /watcher " + w.name));
    if (!need.children.length) need.append(el("p", { class: "empty" }, "Nothing is waiting for you."));
    const did = (t.done || 0), wrong = (t.failed || 0) + (t.unknown || 0);
    out.append(need, el("h2", {}, "Last 24 hours"), el("div", { class: "stats" },
      ...[[did, "done by watchers"], [h.pending.length, "waiting for you"], [wrong, "failed or unclear"], [(t.rejected || 0) + (t.expired || 0), "not allowed"]]
        .map(([n, what]) => el("div", {}, el("b", {}, String(n)), el("span", {}, what)))));
    out.append(el("h2", {}, "Watchers"), el("div", { class: "panel" }, ...(h.watchers.length ? h.watchers.map((w) =>
      row(el("span", {}, el("span", { class: `dot ${w.status}` }), w.name), `${w.status} · last acted ${when(w.last)} · ${w.actions_today} today`)) : [el("p", { class: "empty" }, "None yet.")])));
    out.append(el("h2", {}, "Latest"), el("div", { class: "panel" }, ...(h.recent.length ? h.recent.map((r) =>
      row(`${r.label}${r.items > 1 ? ` ×${r.items}` : ""}`, `${r.name} · ${when(r.at)} · ${r.status}`)) : [el("p", { class: "empty" }, "Nothing yet.")])));
    if (!h.stopped) {
      out.append(el("h2", {}, "Emergency"), el("div", { class: "panel" }, row("Stop everything", "Pauses every watcher and refuses every change. Turning it on again needs your Approve in Telegram.",
        twice("Stop everything", "Press again to stop", async () => { const r = await api("POST", "api/stop", {}); await load(); say(`Stopped. ${r.paused} watcher(s) paused.`); }, "danger"))));
    }
  });
  load();
})();
