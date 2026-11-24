"use strict";
Hub.show(async (out) => {
  const { api, el, row } = Hub;
  const { sections } = await api("GET", "api/overview");
  out.replaceChildren();
  for (const s of sections) out.append(el("h2", {}, s.title), el("div", { class: "panel" }, ...s.rows.map((r) => row(r.now ? `${r.what}: ${r.now}` : r.what, `Change: ${r.where}`))));
});
