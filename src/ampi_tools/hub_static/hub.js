// The hub's access token: it arrives once in the URL fragment (never sent to a server), is kept in this tab's
// sessionStorage (per origin, so other ports on 127.0.0.1 can't read it) and goes with every API call as a header.
"use strict";
const Hub = (() => {
  const KEY = "ampi-hub-token";
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
  async function api(method, path, body) {
    const t = token();
    if (!t) throw new Error("No access: open the hub with scripts/ampi-hub.ps1 on your computer.");
    const opts = { method, headers: { "X-Ampi-Hub": t }, credentials: "omit", cache: "no-store" };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetch(path, opts);
    } catch (e) {
      throw new Error("The hub isn't reachable: is the SSH window (scripts/ampi-hub.ps1) still open?");
    }
    let data = {};
    try { data = await resp.json(); } catch (e) { /* not JSON */ }
    if (!resp.ok) throw new Error(data.error || `The hub answered ${resp.status}.`);
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
