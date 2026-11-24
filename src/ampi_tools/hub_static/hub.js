// On the web (https): a one-time key from the link in Telegram becomes a session cookie this page can't read.
// Over SSH (http://127.0.0.1): the hub's access token: it arrives once in the URL fragment (never sent to a server), is kept in this tab's
// sessionStorage (per origin, so other ports on 127.0.0.1 can't read it) and goes with every API call as a header.
"use strict";
const Hub = (() => {
  const KEY = "ampi-hub-token";
  const web = location.protocol === "https:";
  let memo = "";
  const m = location.hash.match(/(?:^#|&)t=([A-Za-z0-9_-]{20,100})/);
  if (m) {
    memo = m[1];
    try { sessionStorage.setItem(KEY, memo); } catch (e) { /* private mode: keep it in memory */ }
    history.replaceState(null, "", location.pathname);
  }
  function token() {
    if (memo) return memo;
    try { memo = sessionStorage.getItem(KEY) || ""; } catch (e) { memo = ""; }
    return memo;
  }
  const key = web && location.hash.match(/^#k=([A-Za-z0-9_-]{43})$/);
  if (key) history.replaceState(null, "", location.pathname);
  if (web) window.addEventListener("hashchange", () => { if (/^#k=/.test(location.hash)) location.reload(); });
  const ready = key ? login(key[1]) : Promise.resolve();
  ready.catch(() => {});
  async function login(k) {
    const r = await fetch("api/login", { method: "POST", headers: { "X-Ampi-Hub": "web", "Content-Type": "application/json" },
      credentials: "same-origin", cache: "no-store", body: JSON.stringify({ k }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "Couldn't sign in.");
  }
  const PAGES = [["index", "Home", "./"], ["abilities", "Abilities"], ["topics", "Topics"], ["watchers", "Watchers"], ["activity", "Activity"],
    ["given", "Given"], ["setup", "Setup"], ...(web ? [] : [["address-book", "Address book"]])];
  document.addEventListener("DOMContentLoaded", () => {
    const nav = document.getElementById("nav"), on = nav.dataset.page;
    nav.append(el("a", { class: "brand", href: "./" }, "Ampi"));
    for (const [id, name, url] of PAGES) {
      nav.append(el("a", { class: id === on ? "item on" : "item", href: url || id, id: `nav-${id}`,
        ...(id === on ? { "aria-current": "page" } : {}) }, name));
    }
    nav.append(el("span", { class: "grow" }));
    const cur = nav.querySelector(".on");
    if (cur && nav.scrollWidth > nav.clientWidth) nav.scrollLeft = cur.offsetLeft - 40;
    if (!web) return;
    const b = el("button", { class: "btn", type: "button" }, "Sign out");
    nav.append(b);
    b.addEventListener("click", async () => {
      try { await api("POST", "api/logout", {}); } catch (e) {
        if (e.status !== 401) { say(`Not signed out: ${e.message}`, true); return; }
      }
      document.querySelector("main").textContent = "Signed out. Send /dashboard to Ampi for a new link.";
      b.remove();
    });
  });
  async function api(method, path, body) {
    if (web) await ready.catch(() => {});  // (a link used before: the session may still be there)
    const t = web ? "web" : token();
    if (!t) throw new Error("No access: open the hub with scripts/ampi-hub.ps1 on your computer.");
    const opts = { method, headers: { "X-Ampi-Hub": t }, credentials: web ? "same-origin" : "omit", cache: "no-store" };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetch(path, opts);
    } catch (e) {
      throw new Error(web ? "The server isn't reachable." : "The hub isn't reachable: is the SSH window (scripts/ampi-hub.ps1) still open?");
    }
    let data = {};
    try { data = await resp.json(); } catch (e) { /* not JSON */ }
    if (!resp.ok) throw Object.assign(new Error(data.error || `The hub answered ${resp.status}.`), { status: resp.status });
    return data;
  }
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const c of children) if (c !== null && c !== undefined) node.append(c);  // text stays text
    return node;
  }
  function say(text, bad) {
    const m = document.getElementById("msg");
    m.textContent = text;
    m.classList.toggle("err", !!bad);
    m.classList.toggle("hidden", !text);
    if (text) m.scrollIntoView({ block: "nearest" });
  }
  const when = (t) => {
    if (!t) return "never";
    const d = new Date(t * 1000), same = d.toDateString() === new Date().toDateString();
    return d.toLocaleString([], same ? { hour: "2-digit", minute: "2-digit" } : { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  };
  const row = (title, note, ...right) => el("div", { class: "row2 wrap" }, el("div", { class: "k" }, title, note ? el("small", {}, note) : null), ...right);
  const button = (text, fn, cls) => {
    const b = el("button", { class: `btn ${cls || ""}`.trim(), type: "button" }, text);
    b.addEventListener("click", async () => {
      b.disabled = true;
      try { await fn(b); } catch (e) { say(e.message, true); } finally { b.disabled = false; }
    });
    return b;
  };
  // Asks for a second press within a few seconds.
  const twice = (text, sure, fn, cls) => {
    const b = button(text, async () => {
      if (b.textContent !== sure) {
        b.textContent = sure;
        b.classList.add("armed");
        setTimeout(() => { b.textContent = text; b.classList.remove("armed"); }, 4000);
        return;
      }
      await fn();
    }, cls);
    return b;
  };
  const told = (r, done) => say(r.approval ? `Request #${r.approval} waits for your Approve in Telegram (30 minutes).` : done);
  const show = async (fn) => {
    const out = document.getElementById("out");
    try { await fn(out); } catch (e) { if (out.querySelector(".note")) out.replaceChildren(); say(e.message, true); }
  };
  const WORDS = { off: "Off", ask: "Ask", default: "Usual", report: "Report" };
  const seg = (label, value, modes, fn) => {
    const box = el("div", { class: "seg", role: "radiogroup", "aria-label": label });
    const set = (m) => { for (const b of box.children) { b.setAttribute("aria-checked", String(b.dataset.m === m)); b.tabIndex = b.dataset.m === m ? 0 : -1; } };
    for (const m of ["off", "ask", "default", "report"].filter((x) => modes.includes(x) || x === value)) {
      const b = el("button", { type: "button", role: "radio", "data-m": m }, WORDS[m]);
      b.addEventListener("click", () => { set(m); fn(m); });
      b.addEventListener("keydown", (e) => {
        const next = { ArrowRight: b.nextElementSibling, ArrowLeft: b.previousElementSibling }[e.key];
        if (next) { e.preventDefault(); next.focus(); next.click(); }
      });
      box.append(b);
    }
    set(value);
    return box;
  };
  return { api, el, token, say, when, row, button, twice, told, show, seg, WORDS };
})();
