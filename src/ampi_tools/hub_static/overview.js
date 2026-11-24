"use strict";
(async () => {
  const { el, api, say } = Hub, out = document.getElementById("out");
  try {
    for (const s of (await api("GET", "/api/overview")).sections) {
      out.append(el("h2", {}, s.title), el("div", { class: "panel" }, ...s.rows.map((r) =>
        el("div", { class: "kv" }, r.now ? `${r.what}: ${r.now}` : r.what, el("small", {}, `Change: ${r.where}`)))));
    }
  } catch (e) { say(e.message, true); }
})();
