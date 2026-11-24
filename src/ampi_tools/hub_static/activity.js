"use strict";
(() => {
  const { api, el, show, when, button } = Hub;
  let name = "", op = "", status = "", last = null;
  try { name = decodeURIComponent((location.hash.match(/^#w=(.+)$/) || [])[1] || ""); } catch (e) { /* all */ }
  const q = (more) => { const p = new URLSearchParams(); for (const [k, v] of Object.entries({ name, op, status, ...more })) if (v) p.set(k, v); return p.toString(); };
  const act = (r) => el("details", { class: "kv" }, el("summary", {}, `${r.label}${r.items > 1 ? ` ×${r.items}` : ""}`,
    el("span", { class: "note" }, `${r.name} · ${when(r.at)} · ${r.status}`)), el("pre", {}, (r.detail.length ? r.detail.join("\n") : r.args_summary) + (r.result && r.status !== "done" ? `\n→ ${r.result}` : "")));
  const load = () => show(async (out) => {
    const [a, w] = await Promise.all([api("GET", `api/activity?${q()}`), api("GET", "api/watchers")]);
    const sel = (label, value, opts, fn) => {
      const s = el("select", { "aria-label": label }, ...opts.map(([v, t]) => { const o = el("option", { value: v }, t); o.selected = v === value; return o; }));
      s.addEventListener("change", () => { fn(s.value); load(); });
      return s;
    };
    out.replaceChildren(el("div", { class: "tools" },
      sel("Watcher", name, [["", "All watchers"], ...w.watchers.map((x) => [x.name, x.name])], (v) => { name = v; }),
      sel("Action", op, [["", "All actions"], ...[...new Map([...(op ? [[op, op]] : []), ...a.watchers.map((r) => [r.op, r.label])])]], (v) => { op = v; }),
      sel("Result", status, [["", "Any result"], ["done", "Done"], ["requested", "Asked"], ["rejected", "Rejected"], ["expired", "Not answered"], ["failed", "Failed"], ["unknown", "Unclear"]], (v) => { status = v; })));
    const box = el("div", { class: "panel" });
    const more = button("Load more", async () => { const x = await api("GET", `api/activity?${q({ before: last })}`); add(x.watchers); });
    const add = (rows) => { box.append(...rows.map(act)); last = rows.length ? rows[rows.length - 1].id : last; if (rows.length < 60) more.remove(); };
    out.append(el("h2", {}, "Watchers"), box, more);
    add(a.watchers);
    if (!box.children.length) box.append(el("p", { class: "empty" }, "Nothing here."));
    const list = (title, rows) => out.append(el("h2", {}, title), el("div", { class: "panel" }, ...(rows.length ? rows : [el("p", { class: "empty" }, "Nothing yet.")])));
    list("Asked you", a.chat.map((r) => el("details", { class: "kv" }, el("summary", {}, `#${r.id} · ${r.status}`, el("span", { class: "note" }, when(r.at))), el("pre", {}, r.summary))));
    list("Permission changes", a.changes.map((r) => el("div", { class: "kv" }, `${r.op}: ${r.old} → ${r.new}`, el("small", {}, `${r.scope} · ${when(r.at)} · ${r.how}`))));
  });
  load();
})();
