"use strict";
(() => {
  const { api, el, show, say, when, row, twice } = Hub;
  const TITLES = { telegram: "Writing in Telegram as you", site: "Filling in your details on websites", watcher: "Watchers, with buttons in Telegram" };
  const load = () => show(async (out) => {
    const { given } = await api("GET", "api/given");
    out.replaceChildren();
    for (const [kind, title] of Object.entries(TITLES)) {
      const rows = given.filter((g) => g.kind === kind);
      out.append(el("h2", {}, title), el("div", { class: "panel" }, ...(rows.length ? rows.map((g) => row(g.what, g.until ? `until ${when(g.until)}` : "until the watcher changes",
        twice("Remove", "Press again", async () => { await api("POST", "api/revoke", { kind: g.kind, key: g.key }); await load(); say("Removed."); }, "danger"))) : [el("p", { class: "empty" }, "Nothing.")])));
    }
    out.append(el("p", { class: "note" }, "Actions you set to “Report” for a watcher are under Abilities."));
  });
  load();
})();
