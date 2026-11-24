// Nothing happens without the owner's tap, and nothing a page says is listened to (no content scripts, no messages).
'use strict';
(function () {
  const $ = (id) => document.getElementById(id);
  const say = (s) => { $('note').textContent = s; };
  const only = (id) => { for (const s of ['setup', 'give', 'hand']) $(s).hidden = s !== id; };
  let tab = null, site = null, key = null, text = '';

  // The key can only encrypt, and can't be read out again (a CryptoKey that isn't extractable).
  function db() {
    return new Promise((ok, no) => {
      const r = indexedDB.open('ampi', 1);
      r.onupgradeneeded = () => r.result.createObjectStore('keys');
      r.onsuccess = () => ok(r.result);
      r.onerror = () => no(r.error);
    });
  }
  async function store(mode, f) {
    const d = await db();
    return new Promise((ok, no) => {
      const t = d.transaction('keys', mode), r = f(t.objectStore('keys'));
      t.oncomplete = () => ok(r.result);
      t.onerror = () => no(t.error);
    });
  }

  function show() {
    say('');
    $('reset').hidden = !key;
    if (!key) { only('setup'); return; }
    if (site.why) { only(''); say(site.why); return; }
    only('give');
    $('host').textContent = site.host;
    $('what').textContent = 'Ampi can then use your account there as you, until its browser is closed. Ask it for exactly this address. Some '
      + 'sites then sign you out here. Only signing out on the site ends it everywhere.';
  }

  async function start() {
    // A permission that was left over (the popup was closed while Firefox asked for it).
    const had = (await browser.permissions.getAll()).origins || [];
    if (had.length) await browser.permissions.remove({ origins: had }).catch(() => {});
    [tab] = await browser.tabs.query({ active: true, currentWindow: true });
    site = Ampi.site(tab && tab.url, tab && tab.incognito);
    key = await store('readonly', (s) => s.get('key')) || null;
    show();
  }

  $('save').addEventListener('click', async () => {
    const k = await Ampi.readKey($('key').value);
    $('key').value = '';
    if (!k) { say('That isn\'t the key from the Ampi app.'); return; }
    await store('readwrite', (s) => s.put(k, 'key'));
    key = k;
    show();
  });

  $('reset').addEventListener('click', async () => {
    await store('readwrite', (s) => s.delete('key'));
    key = null;
    show();
  });

  $('go').addEventListener('click', async () => {
    const origins = ['https://' + site.host + '/*'];
    // Asked for first: it needs this very tap.
    const asked = browser.permissions.request({ origins: origins });
    $('go').disabled = true;
    try {
      if (!await asked) { say('Not given.'); return; }
      const at = [...new Set(['https://' + site.host + '/', 'https://' + site.host + site.path])];
      const found = [];
      for (const url of at) found.push(...await browser.cookies.getAll({ url: url, storeId: tab.cookieStoreId || 'firefox-default' }));
      const picked = Ampi.pick(found, site.host);
      text = await Ampi.seal(key, site.host, site.path, picked, Date.now());
      if (!text) { say(picked.length ? 'That site has too much to give.' : 'There is nothing to give: are you signed in there?'); return; }
      only('hand');
      $('done').textContent = 'Ready: ' + picked.length + ' cookies of ' + site.host + '.';
      $('share').hidden = !navigator.share;
    } catch (e) {
      say('It didn\'t work: ' + String(e && e.message || e).slice(0, 200));
    } finally {
      $('go').disabled = false;
      browser.permissions.remove({ origins: origins }).catch(() => {});
    }
  });

  $('share').addEventListener('click', () => {
    navigator.share({ text: text }).catch(() => say('Use "Copy instead".'));
  });

  $('copy').addEventListener('click', () => {
    navigator.clipboard.writeText(text).then(() => say('Copied.'), () => say('It couldn\'t be copied.'));
  });

  start().catch((e) => say('It didn\'t work: ' + String(e && e.message || e).slice(0, 200)));
})();
