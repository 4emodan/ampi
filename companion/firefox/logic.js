// What is given and how it is sealed (no browser extension API in here: tested in a browser, opened again by SessionBox.java).
'use strict';
var Ampi = (function () {
  const TAG = 'ampi-session2';
  const MAX = 80, MAX_VALUE = 4096, MAX_PLAIN = 30000;
  // Cookies that vouch for "a person in this very browser" (the known ones). Handing them on would get around a site's bot check: not
  // for this. (CLEARANCE in src/ampi_tools/checkout.py is the same.)
  const CLEARANCE = /^(cf_clearance|__cf_bm|__cflb|__cfruid|_cfuvid|cf_chl_.*|_abck|ak_bmsc|bm_(sz|sv|mi|so|s|ss|lso)|sbsd(_.*)?|sec_cpt|datadome|_px(\d|hd|vid|de|ff.*)?|pxcts|reese84|___utmv.*|incap_ses_.*|visid_incap_.*|nlbi_.*|aws-waf-token|kp_uid.*|x-kpsdk-.*|__ddg.*|_grecaptcha|rc::.*|frcaptcha.*|h-captcha.*|_vcrcs|__uzm.*|uzm.*|rbzid.*|rbzsessionid|sucuri_.*|.*anubis-auth.*|acw_sc__.*|acw_tc|_fs_ch_.*|qrator_.*|d_[a-z]id|arkose.*|_iidt|geetest.*)$/i;
  const TRACKING = /^(_ga(_.*)?|_gid|_gat(_.*)?|_gcl_.*|_fbp|_fbc|_hj[A-Z].*|_clck|_clsk|_uet(sid|vid)|_pin_unauth|_scid(_r)?|_tt_enable_cookie|_ttp|__utm[abcz]|__utmt)$/;
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

  // Of the cookies Firefox would send there: the ones that can be given. (If there are too many, the ones a page can't read go first.)
  function pick(cookies, host) {
    const out = [], seen = new Set();
    for (const c of cookies) {
      const domain = String(c.domain || '').replace(/^\./, '').toLowerCase();
      const own = domain === host || (!c.hostOnly && domain.includes('.') && host.endsWith('.' + domain));
      if (!own || c.hostOnly && domain !== host || c.partitionKey || CLEARANCE.test(c.name) || TRACKING.test(c.name)) continue;
      if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}$/.test(c.name) || !/^[\x20-\x3a\x3c-\x7e]*$/.test(c.value) || c.value.length > MAX_VALUE) continue;
      if (!/^\/[\x21-\x3a\x3c-\x7e]{0,500}$/.test(c.path || '/')) continue;
      const key = [c.name, domain, c.path].join(' ');
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ n: c.name, v: c.value, d: domain, o: !!c.hostOnly, p: c.path || '/', s: !!c.secure, h: !!c.httpOnly,
        x: { lax: 'Lax', strict: 'Strict', no_restriction: 'None' }[c.sameSite] || '' });
    }
    return out.length <= MAX ? out : out.filter((c) => c.h).concat(out.filter((c) => !c.h)).slice(0, MAX);
  }

  // The widest name any of them is sent to ('' when only to that host).
  function scope(picked) {
    const wide = picked.filter((c) => !c.o).map((c) => c.d).sort((a, b) => a.length - b.length);
    return wide.length ? wide[0] : '';
  }

  // What the app gave: its public key, and a secret that only this extension has. With them one can be sealed, none can be opened.
  async function readKey(text) {
    const m = /ampi-key2\.([A-Za-z0-9_-]{43})\.([A-Za-z0-9_-]{300,800})(?![A-Za-z0-9_-])/.exec(text || '');
    if (!m) return null;
    try {
      return { mac: await crypto.subtle.importKey('raw', unb64(m[1]), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']),
        wrap: await crypto.subtle.importKey('spki', unb64(m[2]), { name: 'RSA-OAEP', hash: 'SHA-1' }, false, ['encrypt']) };
    } catch (e) { return null; }
  }

  const join = (...parts) => { const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0)); let at = 0; for (const p of parts) { out.set(p, at); at += p.length; } return out; };
  const bytes = (s) => new TextEncoder().encode(s);

  async function seal(key, host, path, picked, now) {
    const plain = bytes(JSON.stringify({ p: path, c: picked }));
    if (!picked.length || plain.length > MAX_PLAIN) return null;
    const head = [host, String(now), hex(crypto.getRandomValues(new Uint8Array(16))), String(picked.length), scope(picked)].join('\n');
    const random = crypto.getRandomValues(new Uint8Array(32)), iv = crypto.getRandomValues(new Uint8Array(12)), bound = bytes(TAG + '\n' + head);
    const wrapped = new Uint8Array(await crypto.subtle.encrypt({ name: 'RSA-OAEP' }, key.wrap, random));
    const raw = await crypto.subtle.sign('HMAC', key.mac, join(bound, bytes('\n'), random));
    const aes = await crypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['encrypt']);
    const enc = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv: iv, additionalData: bound }, aes, plain));
    return TAG + '.' + b64(bytes(head)) + '.' + b64(join(wrapped, iv, enc));
  }

  return { site: site, pick: pick, scope: scope, readKey: readKey, seal: seal };
})();
