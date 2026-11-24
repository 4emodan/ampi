"use strict";
(async () => {
  const { el, api, say } = Hub, out = document.getElementById("out");
  const when = (t) => new Date(t * 1000).toLocaleString();
  const block = (title, rows) => out.append(el("details", { class: "panel", open: "" }, el("summary", {}, title),
    ...(rows.length ? rows : [el("p", { class: "note" }, "Nothing yet.")])));
  const row = (head, text) => el("div", { class: "kv" }, head, el("pre", {}, text || ""));
  try {
    const a = await api("GET", "/api/activity");
    block("Watchers", a.watchers.map((r) => row(`${when(r.at)} · ${r.name} · ${r.op} · ${r.status}`,
      (r.detail.length ? r.detail.join("\n") : r.args_summary) + (r.status === "failed" && r.result ? `\n→ ${r.result}` : ""))));
    block("Asked you", a.chat.map((r) => row(`${when(r.at)} · #${r.id} · ${r.status}`, r.summary)));
    block("Permission changes", a.changes.map((r) => row(`${when(r.at)} · ${r.scope} · ${r.op}: ${r.old} → ${r.new}`, r.how)));
  } catch (e) { say(e.message, true); }
})();
