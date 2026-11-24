"use strict";
(() => {
  const { api, el, show, say, seg, button, WORDS } = Hub;
  const RANK = { off: 0, ask: 1, default: 2, report: 3 };
  let scope = "chat", data, want = {}, now = {}, text = "", only = false, places = [];
  try { scope = decodeURIComponent((location.hash.match(/^#s=(.+)$/) || [])[1] || "chat"); } catch (e) { /* as it was */ }
  const changes = () => Object.fromEntries(Object.keys(want).filter((k) => want[k] !== now[k]).map((k) => [k, [now[k], want[k]]]));
  async function load(open) {
    if (Object.keys(changes()).length && !open) want = {};
    data = await api("GET", `api/abilities?scope=${encodeURIComponent(scope)}`);
    want = {}; now = {};
    history.replaceState(null, "", scope === "chat" ? location.pathname : `#s=${encodeURIComponent(scope)}`);
    draw(open || new Set());
  }
  function line(op, label, note, mode, modes, effective) {
    now[op] = mode;
    if (!(op in want)) want[op] = mode;
    const wait = data.waiting[op] ? ` · waiting for your Approve: ${WORDS[data.waiting[op]] || data.waiting[op]}` : "";
    const held = effective && effective !== mode && RANK[effective] < RANK[want[op]] ? ` · now ${WORDS[effective].toLowerCase()}, by a wider setting` : "";
    const r = el("div", { class: "oprow" }, el("div", { class: "k" }, label, el("small", {}, note + held + wait)),
      seg(label, want[op], modes, (m) => { want[op] = m; r.classList.toggle("changed", want[op] !== now[op]); bar(); }));
    r.classList.toggle("changed", want[op] !== now[op]);
    return r;
  }
  let bar = () => {};
  function draw(open) {
    const out = document.getElementById("out");
    const q = text.toLowerCase();
    out.replaceChildren();
    const pick = el("select", { "aria-label": "Where" }, ...places.map(([id, name]) => {
      const o = el("option", { value: id }, name);
      o.selected = id === scope;
      return o;
    }));
    pick.addEventListener("change", () => {
      if (Object.keys(changes()).length && !confirm("Leave without saving?")) { pick.value = scope; return; }
      scope = pick.value; show(() => load());
    });
    const search = el("input", { type: "search", placeholder: "Find an action", "aria-label": "Find an action", value: text });
    const box = el("div", {});
    search.addEventListener("input", () => { text = search.value; list(box, text.toLowerCase(), opened(box)); });
    const tick = el("input", { type: "checkbox" });
    tick.checked = only;
    tick.addEventListener("change", () => { only = tick.checked; list(box, text.toLowerCase(), opened(box)); });
    out.append(el("div", { class: "tools" }, pick, search, el("label", { class: "check" }, tick, "Only what I set")));
    const watcher = scope.startsWith("watcher:");
    out.append(el("p", { class: "note" }, watcher
      ? "Usual: asks unless you allowed it for this watcher. Report: does it and tells you afterwards."
      : scope === "chat" ? "Usual: the built-in rule. This holds in every chat, group and topic."
        : "A topic can only be stricter than “Everywhere”."));
    if (!watcher) {
      out.append(el("div", { class: "panel" }, line("changes.*", "Everything that changes something",
        "Sending, deleting, booking, editing, alarms. Off = read only here.", data.changes, ["off", "ask", "default"], null)));
    }
    list(box, q, open);
    const save = button("Save", send, "primary"), hint = el("span", { class: "note" });
    bar = () => {
      const c = Object.values(changes());
      save.disabled = !c.length;
      hint.textContent = !c.length ? "" : c.some(([a, b]) => RANK[b] > RANK[a])
        ? `${c.length} changed. What is looser waits for your Approve in Telegram.` : `${c.length} changed. Takes effect at once.`;
    };
    out.append(box, el("div", { class: "bar" }, save, button("Undo", async () => { want = {}; draw(opened(box)); }), hint));
    bar();
  }
  const opened = (box) => new Set([...box.querySelectorAll("details[open]")].map((d) => d.dataset.a));
  function list(box, q, open) {
    box.replaceChildren();
    for (const a of data.adapters) {
      const hit = (o) => !q || `${o.op} ${o.label} ${a.name}`.toLowerCase().includes(q);
      const mine = (o) => !only || o.mode !== "default" || (o.op in want && want[o.op] !== o.mode);
      const ops = a.ops.filter((o) => hit(o) && mine(o)).sort((x, y) => y.write - x.write || (x.op < y.op ? -1 : 1));
      const whole = (!q || a.name.toLowerCase().includes(q)) && (!only || a.mode !== "default");
      if (!ops.length && !whole) continue;
      if (!a.ops.length && a.never.length) continue;
      const n = a.ops.filter((o) => o.mode !== "default").length;
      const d = el("details", { class: "panel" }, el("summary", {}, a.name, " ",
        a.mode === "off" ? el("span", { class: "tag off" }, "off") : n ? el("span", { class: "tag" }, `${n} set`) : null));
      d.dataset.a = a.adapter;
      d.open = open.has(a.adapter) || !!q || only;
      const body = el("div", {});
      if (whole || ops.length) body.append(line(`${a.adapter}.*`, `All of ${a.name}`, "Off: nothing of it works here.", a.mode, ["off", "default"], null));
      body.append(...ops.map((o) => line(o.op, o.label || o.op, `${o.rule}${o.label ? ` · ${o.op}` : ""}`, o.mode, o.modes, o.now)));
      if (a.never.length) body.append(el("p", { class: "note" }, `Never here: ${a.never.join(", ")}`));
      d.append(body);
      box.append(d);
    }
    if (!box.children.length) box.append(el("p", { class: "empty" }, only ? "Nothing is set here." : "Nothing matches."));
  }
  async function send() {
    const c = changes(), open = opened(document.getElementById("out"));
    if (!Object.keys(c).length) return;
    try {
      const r = await api("POST", "api/set", { scope, changes: c });
      await load(open);
      say([r.changed ? `${r.changed} made stricter.` : "", r.approval ? `Request #${r.approval} waits for your Approve in Telegram (30 minutes), then reload.` : ""].join(" ").trim());
    } catch (e) { await load(open).catch(() => {}); say(e.message, true); }
  }
  show(async () => {
    const [t, w] = await Promise.all([api("GET", "api/topics"), api("GET", "api/watchers")]);
    places = [["chat", "Everywhere"], ...t.topics.map((x) => [x.scope, `Topic: ${x.title || x.where}`]), ...w.watchers.map((x) => [x.scope, `Watcher: ${x.name}`])];
    if (!places.some(([id]) => id === scope)) scope = "chat";
    await load();
  });
})();
