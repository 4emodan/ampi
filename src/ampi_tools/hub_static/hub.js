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
  if (web) document.addEventListener("DOMContentLoaded", () => {
    for (const a of document.querySelectorAll('a[href="address-book"]')) a.remove();  // only over SSH
    const b = document.getElementById("out-btn");
    b.classList.remove("hidden");
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
  return { api, el, token, say };
})();
