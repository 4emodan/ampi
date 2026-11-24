"use strict";
(() => {
  const { api, el, show, say, when, button, twice, told, WORDS } = Hub;
  const load = () => show(async (out) => {
    const { topics } = await api("GET", "api/topics");
    out.replaceChildren();
    if (!topics.length) out.append(el("p", { class: "empty" }, "None yet. A conversation shows up here once you wrote in it."));
    for (const t of topics) {
      const role = el("textarea", { maxlength: "1000", "aria-label": "Role", placeholder: "e.g. You keep my household budget. Answer in two lines." });
      role.value = t.role;
      const tags = [t.changes !== "default" ? el("span", { class: t.changes === "off" ? "tag off" : "tag" }, `changes: ${WORDS[t.changes].toLowerCase()}`) : null,
        ...t.off.map((o) => el("span", { class: "tag off" }, `${o} off`))];
      out.append(el("details", { class: "panel" }, el("summary", {}, t.title || t.where, " ", ...tags.flatMap((x) => (x ? [x, " "] : []))),
        el("p", { class: "note" }, `${t.where} · last used ${when(t.seen)}${t.watchers.length ? ` · watchers: ${t.watchers.join(", ")}` : ""}`),
        el("label", {}, "Role: your instructions for Ampi here"), role,
        t.waiting.role ? el("p", { class: "warn" }, `Waiting for your Approve: ${t.waiting.role}`) : null,
        el("div", { class: "actions" },
          button("Save role", async () => { told(await api("POST", "api/role", { scope: t.scope, role: role.value })); }, "primary"),
          el("a", { class: "btn", href: `abilities#s=${encodeURIComponent(t.scope)}` }, "Limits"),
          el("span", { class: "spacer" }),
          t.set || t.role || t.watchers.length ? null : twice("Forget", "Press again", async () => { await api("POST", "api/forget", { scope: t.scope }); await load(); say("Forgotten."); }))));
    }
    out.append(el("p", { class: "note" }, "In Telegram: /topic. Notes and logs are shared between topics: a role or a limit doesn't hide one topic's history from another."));
  });
  load();
})();
