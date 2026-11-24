"use strict";
(() => {
  const { api, el } = Hub;
  const $ = (id) => document.getElementById(id);
  const ADDRESS = ["line1", "line2", "postal", "city", "region", "country"];
  let places = [];
  let current = null;       // the place being edited (null: a new one)
  let savedAddress = "";    // the address as loaded, to notice a change
  let savedLocation = "";

  function say(text, isError) {
    const box = $("msg");
    box.textContent = text;
    box.className = "msg" + (isError ? " err" : "");
    if (!isError) setTimeout(() => { if (box.textContent === text) box.className = "msg hidden"; }, 4000);
  }

  function oneLine(p) {
    const a = p.address;
    return [a.line1, a.line2, [a.postal, a.city].filter(Boolean).join(" "), a.country].filter(Boolean).join(", ");
  }

  function renderList() {
    const list = $("list");
    list.replaceChildren();
    if (!places.length) list.append(el("li", { class: "empty" }, "No places yet."));
    for (const p of places) {
      const tags = [p.search ? "searches" : "", p.location ? "location" : "", p.timezone || ""].filter(Boolean);
      list.append(el("li", {},
        el("button", { type: "button", class: current && current.name === p.name ? "on" : "", onclick: () => edit(p) },
          el("div", { class: "n" }, p.name + (p.company ? ` · ${p.company}` : "")),
          el("div", { class: "a" }, oneLine(p) || "no address"),
          el("div", { class: "tags" }, tags.join(" · ")))));
    }
  }

  function addressNow() {
    return ADDRESS.map((k) => $("f-" + k).value.trim()).join("|");
  }

  function checkStale() {
    const loc = $("f-location").value.trim();
    const stale = loc && loc === savedLocation && addressNow() !== savedAddress;
    $("stale").classList.toggle("hidden", !stale);
  }

  function edit(p) {
    current = p;
    $("pick").classList.add("hidden");
    $("form").classList.remove("hidden");
    $("f-name").value = p ? p.name : "";
    $("f-company").value = p ? p.company : "";
    for (const k of ADDRESS) $("f-" + k).value = p ? p.address[k] : "";
    $("f-location").value = p ? p.location : "";
    $("f-timezone").value = p ? p.timezone : "";
    $("f-search").checked = p ? p.search : true;
    $("f-revealable").checked = p ? p.revealable : true;
    $("delete").classList.toggle("hidden", !p);
    $("found").classList.add("hidden");
    savedAddress = addressNow();
    savedLocation = $("f-location").value.trim();
    checkStale();
    homeNote();
    renderList();
    $("f-name").focus();
  }

  function homeNote() {
    $("home-note").classList.toggle("hidden", $("f-name").value.trim() !== "home");
  }

  function formBody() {
    const address = {};
    for (const k of ADDRESS) address[k] = $("f-" + k).value.trim();
    address.country = address.country.toUpperCase();
    return {
      name: $("f-name").value.trim(), company: $("f-company").value.trim(), address,
      location: $("f-location").value.trim(), timezone: $("f-timezone").value.trim(),
      search: $("f-search").checked, revealable: $("f-revealable").checked,
    };
  }

  async function load(selectName) {
    try {
      places = (await api("GET", "api/places")).places;
    } catch (e) {
      say(e.message, true);
      return;
    }
    const pick = places.find((p) => p.name === selectName);
    if (pick) edit(pick);
    else if (current && !places.find((p) => p.name === current.name)) { current = null; cancel(); }
    renderList();
  }

  function cancel() {
    current = null;
    $("form").classList.add("hidden");
    $("pick").classList.remove("hidden");
    renderList();
  }

  async function save(ev) {
    ev.preventDefault();
    const body = formBody();
    if (!/^[a-z][a-z0-9-]{0,30}$/.test(body.name)) {
      say("Name: lowercase letters, digits and -, starting with a letter (e.g. gym, mums-place).", true);
      return;
    }
    if (body.location && body.location === savedLocation && addressNow() !== savedAddress &&
        !confirm("The address changed but the location didn't: searches would still start at the old place. Save anyway?")) {
      return;
    }
    if (current && current.name !== body.name &&
        !confirm(`Rename “${current.name}” to “${body.name}”? Ampi, watchers and “--at ${current.name}” use it by name; ` +
                 "watchers that use it lose it, and its site permissions go.")) {
      return;
    }
    $("save").disabled = true;
    try {
      let saved;
      if (current) saved = (await api("PUT", `api/places/${encodeURIComponent(current.name)}`, { ...body, rev: current.rev })).place;
      else saved = (await api("POST", "api/places", body)).place;
      say(`Saved ${saved.name}.`);
      await load(saved.name);
    } catch (e) {
      say(e.message, true);
    } finally {
      $("save").disabled = false;
    }
  }

  async function remove() {
    if (!current) return;
    const extra = ["home", "work"].includes(current.name) ? " Ampi and watchers use it by name." : "";
    if (!confirm(`Delete “${current.name}” and its site permissions?${extra}`)) return;
    try {
      await api("DELETE", `api/places/${encodeURIComponent(current.name)}`, { rev: current.rev });
      say(`Deleted ${current.name}.`);
      current = null;
      cancel();
      await load();
    } catch (e) {
      say(e.message, true);
    }
  }

  async function find() {
    const a = formBody().address;
    const query = [a.line1, a.line2, a.postal, a.city, a.region, a.country].filter(Boolean).join(", ");
    if (!a.line1 && !a.city) { say("Type the street and city first.", true); return; }
    $("find").disabled = true;
    try {
      const r = await api("POST", "api/geocode", { address: query });
      $("f-location").value = r.location;
      const note = $("found");
      note.replaceChildren("Found: " + (r.title || "a match") + " · ");
      if (r.url && /^https:\/\//.test(r.url)) {
        note.append(el("a", { href: r.url, target: "_blank", rel: "noopener noreferrer" }, "Google Maps"));
      } else {
        note.append("Google Maps");
      }
      note.classList.remove("hidden");
      savedLocation = r.location;  // a fresh lookup for this address; a later address edit makes it stale again
      savedAddress = addressNow();
      checkStale();
    } catch (e) {
      say(e.message, true);
    } finally {
      $("find").disabled = false;
    }
  }

  try {
    const zones = Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : [];
    const list = $("zones");
    for (const z of zones) list.append(el("option", { value: z }));
  } catch (e) { /* no list: free text still works */ }

  $("add").addEventListener("click", () => edit(null));
  $("cancel").addEventListener("click", cancel);
  $("delete").addEventListener("click", remove);
  $("find").addEventListener("click", find);
  $("form").addEventListener("submit", save);
  $("f-name").addEventListener("input", homeNote);
  for (const id of ["f-location", ...ADDRESS.map((k) => "f-" + k)]) $(id).addEventListener("input", checkStale);
  load();
})();
