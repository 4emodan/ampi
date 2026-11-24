"use strict";
// One scope's switches: used by the Abilities page and by each watcher's card.
const RANK = { off: 0, ask: 1, default: 2, report: 3 };
const WORDS = { off: "Off", ask: "Ask each time", default: "Built-in rule", report: "Do it, report after" };
async function abilities(scope, box) {
  const { el, api, say } = Hub;
  const data = await api("GET", `api/abilities?scope=${encodeURIComponent(scope)}`);
  const now = {}, want = {};
  const save = el("button", { class: "btn primary", type: "button", onclick: send }, "Save");
  const hint = el("span", { class: "note" });
  save.disabled = true;
  const open = new Set([...box.querySelectorAll("details[open]")].map((d) => d.dataset.a));
  box.replaceChildren();
  function changes() {
    const c = {};
    for (const op of Object.keys(want)) if (want[op] !== now[op]) c[op] = [now[op], want[op]];
    return c;
  }
  function pick(op, mode, modes) {
    now[op] = want[op] = mode;
    if (!modes.includes(mode)) modes = [...modes, mode];
    const s = el("select", { "aria-label": op }, ...modes.map((m) => {
      const o = el("option", { value: m }, WORDS[m]);
      if (m === mode) o.selected = true;
      return o;
    }));
    s.addEventListener("change", () => {
      want[op] = s.value;
      s.closest(".oprow").classList.toggle("changed", want[op] !== now[op]);
      const c = Object.values(changes());
      save.disabled = !c.length;
      hint.textContent = c.some(([a, b]) => RANK[b] > RANK[a]) ? "Anything looser waits for your Approve in Telegram." : "";
    });
    return s;
  }
  function line(op, label, rule, mode, modes) {
    const wait = data.waiting[op] ? ` · waiting for your Approve: ${WORDS[data.waiting[op]]}` : "";
    return el("div", { class: "oprow" }, el("div", { class: "k" }, label, el("small", {}, rule + wait)), pick(op, mode, modes));
  }
  for (const a of data.adapters) {
    if (!a.ops.length && scope !== "chat" && !["tg-read", "web"].includes(a.adapter)) continue;
    const set = a.ops.filter((o) => o.mode !== "default").length + (a.mode !== "default" ? 1 : 0);
    const d = el("details", { class: "panel" }, el("summary", {}, a.name, el("span", { class: "note" }, set ? `${set} set by you` : "built-in rules")),
      line(`${a.adapter}.*`, `All of ${a.name}`, "Off: nothing of it works", a.mode, ["default", "off"]),
      ...a.ops.sort((x, y) => y.write - x.write || (x.op < y.op ? -1 : 1)).map((o) =>
        line(o.op, o.label ? `${o.label} (${o.op})` : o.op, o.rule, o.mode, o.modes)));
    if (a.never.length) d.append(el("p", { class: "note" }, `Never here: ${a.never.join(", ")}`));
    d.dataset.a = a.adapter;
    d.open = open.has(a.adapter);
    box.append(d);
  }
  box.append(el("div", { class: "bar" }, save, hint));
  async function send() {
    const c = changes();
    if (!Object.keys(c).length) return;
    save.disabled = true;
    try {
      const r = await api("POST", "api/set", { scope, changes: c });
      await abilities(scope, box);
      say([r.changed ? `${r.changed} made stricter.` : "", r.approval ? `Request #${r.approval} waits for your Approve in Telegram (30 minutes), then reload.` : ""].join(" ").trim());
    } catch (e) {
      await abilities(scope, box).catch(() => { save.disabled = false; });
      say(e.message, true);
    }
  }
}
if (location.pathname.endsWith("/abilities")) {
  abilities("chat", document.getElementById("out")).catch((e) => Hub.say(e.message, true));
}
