// What is given and how it is sealed (no browser extension API in here: tested in a browser, opened again by SessionBox.java).
'use strict';
var Ampi = (function () {
  const TAG = 'ampi-session1';
  const MAX = 80, MAX_VALUE = 4096, MAX_PLAIN = 30000;
  // Cookies that vouch for "a person in this very browser". Handing them on would get around a site's bot check: not for this.
  const CLEARANCE = /^(cf_clearance|__cf_bm|__cflb|_cfuvid|cf_chl.*|_abck|bm_.*|ak_bmsc|sec_cpt|datadome|_px.*|pxcts|reese84|incap_ses.*|visid_incap.*|nlbi_.*|aws-waf-token|kp_uid.*|x-kpsdk.*|__ddg.*|_grecaptcha|rc::.*|frcaptcha.*|h-captcha.*)$/i;
  const TRACKING = /^(_ga|_gid|_gat|_gcl|_fbp|_fbc|_hj|_clck|_clsk|_uet|_pin_|_scid|_tt_|_ttp|__utm)/i;
  const b64 = (bytes) => { let s = ''; for (const b of bytes) s += String.fromCharCode(b); return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''); };
  const unb64 = (s) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), (c) => c.charCodeAt(0));
  const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');

  // The site of a tab, or why not.
  function site(url, incognito) {
    let u;
    try { u = new URL(url); } catch (e) { return { why: 'Open the site first.' }; }
    if (incognito) return { why: 'Not from a private tab.' };
    if (u.protocol !== 'https:' || u.port || u.username) return { why: 'Only for https sites.' };
    if (!/^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$/.test(u.hostname)) return { why: 'Not for this address.' };
    const path = /^\/[A-Za-z0-9._~%/+-]{0,200}$/.test(u.pathname) && !u.pathname.includes('..') && !u.pathname.includes('//') ? u.pathname : '/';
    return { host: u.hostname, path: path };
  }

  // Of the cookies Firefox would send there: the ones that can be given.
  function pick(cookies, host) {
    const out = [], seen = new Set();
    for (const c of cookies) {
      const domain = String(c.domain || '').replace(/^\./, '').toLowerCase();
      const own = domain === host || (!c.hostOnly && domain.includes('.') && host.endsWith('.' + domain));
      if (!own || c.hostOnly && domain !== host || c.partitionKey || CLEARANCE.test(c.name) || TRACKING.test(c.name)) continue;
      if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}$/.test(c.name) || !/^[\x20-\x3a\x3c-\x7e]*$/.test(c.value) || c.value.length > MAX_VALUE) continue;
      const key = [c.name, domain, c.path].join(' ');
      if (seen.has(key) || out.length >= MAX) continue;
      seen.add(key);
      out.push({ n: c.name, v: c.value, d: domain, o: !!c.hostOnly, p: c.path || '/', s: !!c.secure, h: !!c.httpOnly,
        x: { lax: 'Lax', strict: 'Strict', no_restriction: 'None' }[c.sameSite] || '' });
    }
    return out;
  }

  // The widest name any of them is sent to ('' when only to that host).
  function scope(picked) {
    const wide = picked.filter((c) => !c.o).map((c) => c.d).sort((a, b) => a.length - b.length);
    return wide.length ? wide[0] : '';
  }

  function readKey(text) {
    const m = /ampi-key1-([A-Za-z0-9_-]{43})(?![A-Za-z0-9_-])/.exec(text || '');
    return m ? crypto.subtle.importKey('raw', unb64(m[1]), 'AES-GCM', false, ['encrypt']) : Promise.resolve(null);
  }

  async function seal(key, host, path, picked, now) {
    const plain = new TextEncoder().encode(JSON.stringify(picked));
    if (!picked.length || plain.length > MAX_PLAIN) return null;
    const head = [host, path, String(now), hex(crypto.getRandomValues(new Uint8Array(16))), String(picked.length), scope(picked)].join('\n');
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const enc = new Uint8Array(await crypto.subtle.encrypt(
      { name: 'AES-GCM', iv: iv, additionalData: new TextEncoder().encode(TAG + '\n' + head) }, key, plain));
    const all = new Uint8Array(12 + enc.length); all.set(iv); all.set(enc, 12);
    return TAG + '.' + b64(new TextEncoder().encode(head)) + '.' + b64(all);
  }

  return { site: site, pick: pick, scope: scope, readKey: readKey, seal: seal };
})();
