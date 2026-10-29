/* window.claude for the hosted dashboard.
   The page was written as a claude.ai artifact and calls window.claude.use('db' | 'user' |
   'downloads' | 'mcp'). This file provides the same calls on top of the app's own /api,
   so index.html runs unchanged. Only the parts of each interface the page uses exist. */
(function () {
  const POLL_MS = 15000;
  /* A page can keep its collections apart from other pages' (the server sets this before loading the shim). */
  const PREFIX = window.SHIM_DB_PREFIX || '';
  const err = (code, message) => Object.assign(new Error(message), { code });

  async function api(method, path, body) {
    const r = await fetch(path, {
      method, credentials: 'same-origin',
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) {
      let msg = r.statusText;
      try { msg = (await r.json()).detail || msg; } catch (e) {}
      throw err(r.status === 403 ? 'permission_denied' : r.status === 503 ? 'unavailable' : 'failed', msg);
    }
    return r.json();
  }

  /* ---------- db: one cached copy per collection, kept fresh by polling ---------- */
  const stores = {};
  function store(name) {
    name = PREFIX + name;
    if (stores[name]) return stores[name];
    const s = stores[name] = { docs: new Map(), cursor: 0, listeners: new Set(), loaded: false, timer: null, busy: null };
    s.refresh = function () {
      if (s.busy) return s.busy;
      s.busy = api('GET', '/api/db/' + encodeURIComponent(name) + '?since=' + s.cursor).then(p => {
        let changed = !s.loaded;
        p.docs.forEach(d => { s.docs.set(d.id, d.data); changed = true; });
        const live = new Set(p.ids);
        [...s.docs.keys()].forEach(id => { if (!live.has(id)) { s.docs.delete(id); changed = true; } });
        s.cursor = p.cursor; s.loaded = true;
        if (changed) s.emit();
      }).finally(() => { s.busy = null; });
      return s.busy;
    };
    s.emit = () => s.listeners.forEach(l => l.fire());
    s.start = function () {
      if (s.timer) return;
      s.timer = setInterval(() => { if (!document.hidden) s.refresh().catch(() => {}); }, POLL_MS);
    };
    document.addEventListener('visibilitychange', () => { if (!document.hidden && s.timer) s.refresh().catch(() => {}); });
    return s;
  }

  function snapshot(s, q) {
    let rows = [...s.docs.entries()].map(([id, data]) => ({ id, data }));
    q.where.forEach(([f, op, v]) => {
      const t = { '<': (a, b) => a < b, '<=': (a, b) => a <= b, '>': (a, b) => a > b, '>=': (a, b) => a >= b, '==': (a, b) => a === b, '!=': (a, b) => a !== b }[op];
      rows = rows.filter(r => t(r.data[f], v));
    });
    if (q.order) {
      const [f, dir] = q.order, k = dir === 'desc' ? -1 : 1;
      rows.sort((a, b) => (a.data[f] < b.data[f] ? -1 : a.data[f] > b.data[f] ? 1 : 0) * k);
    }
    if (q.limit) rows = rows.slice(0, q.limit);
    const docs = rows.map(r => ({ id: r.id, exists: true, data: () => r.data }));
    return { docs, size: docs.length, empty: !docs.length };
  }

  function query(name, q) {
    return {
      where: (f, op, v) => query(name, Object.assign({}, q, { where: q.where.concat([[f, op, v]]) })),
      orderBy: (f, dir) => query(name, Object.assign({}, q, { order: [f, dir || 'asc'] })),
      limit: n => query(name, Object.assign({}, q, { limit: n })),
      async get() { const s = store(name); await s.refresh(); return snapshot(s, q); },
      onSnapshot(next, onErr) {
        const s = store(name);
        const l = { fire: () => { try { next(snapshot(s, q)); } catch (e) { console.error(e); } } };
        s.listeners.add(l);
        s.start();
        if (s.loaded) l.fire(); else s.refresh().catch(e => onErr && onErr(e));
        return () => s.listeners.delete(l);
      },
    };
  }

  function doc(path) {
    const i = path.indexOf('/'), name = path.slice(0, i), id = path.slice(i + 1);
    const url = '/api/db/' + encodeURIComponent(PREFIX + name) + '/' + encodeURIComponent(id);
    const snap = d => ({ id, exists: d !== undefined, data: () => d });
    /* One document straight from the server, so reading a large document never pulls its whole collection. */
    async function fetchOne() {
      const r = await fetch(url, { credentials: 'same-origin' });
      if (r.status === 404) return undefined;
      if (!r.ok) throw err(r.status === 503 ? 'unavailable' : 'failed', r.statusText);
      return (await r.json()).data;
    }
    async function write(plain) {
      await api('PUT', url, plain);
      const s = store(name); s.docs.set(id, plain); s.emit();
    }
    return {
      async set(data) { await write(JSON.parse(JSON.stringify(data))); },
      async update(fields) {
        const cur = await fetchOne();
        if (cur === undefined) throw err('not_found', 'No such document');
        await write(Object.assign({}, cur, JSON.parse(JSON.stringify(fields))));
      },
      async delete() {
        await api('DELETE', url);
        const s = store(name); s.docs.delete(id); s.emit();
      },
      async get() { return snap(await fetchOne()); },
      onSnapshot(next, onErr) {
        const s = store(name);
        const l = { fire: () => { try { next(snap(s.docs.get(id))); } catch (e) { console.error(e); } } };
        s.listeners.add(l);
        s.start();
        if (s.loaded) l.fire(); else s.refresh().catch(e => onErr && onErr(e));
        return () => s.listeners.delete(l);
      },
    };
  }

  const db = { collection: name => query(name, { where: [] }), doc };

  /* ---------- user: Google SSO identity from /api/me ---------- */
  let meP = null;
  const me = () => (meP = meP || api('GET', '/api/me'));
  const nameOf = email => String(email || '').split('@')[0].split(/[._-]+/).filter(Boolean)
    .map(w => w[0].toUpperCase() + w.slice(1)).join(' ');
  const user = {
    async can(scope) { const m = await me(); return scope === 'data.write' ? !!m.can_write : true; },
    async canEdit() { return !!(await me()).can_write; },
    async id() { const m = await me(); return m.email || null; },
    async profiles(ids) {
      const m = await me(), out = {};
      (ids || []).forEach(id => { out[id] = { name: nameOf(id), isMe: !!m.email && id === m.email }; });
      return out;
    },
  };

  /* ---------- downloads: a plain browser download ---------- */
  const downloads = {
    async save({ filename, data }) {
      const blob = data instanceof Blob ? data : new Blob([data]);
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = filename;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    },
  };

  /* ---------- assets: files stored by the backend, read back from /api/blobs/<id> ---------- */
  const assets = {
    async upload(blob, opts) {
      const r = await fetch('/api/blobs', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': (opts && opts.type) || blob.type || 'application/octet-stream' }, body: blob,
      });
      if (!r.ok) {
        let msg = r.statusText;
        try { msg = (await r.json()).detail || msg; } catch (e) {}
        throw err(r.status === 403 ? 'not_granted' : r.status === 413 ? 'too_large' : 'failed', msg);
      }
      return r.json();
    },
  };

  /* ---------- mcp: Metabase `query` through the backend ---------- */
  const mcp = {
    servers: ['Metabase'],
    async callTool(server, tool, args) {
      if (server !== 'Metabase' || tool !== 'query') throw err('not_in_manifest', 'Only Metabase query is available.');
      try {
        return await api('POST', '/api/metabase/query', args);
      } catch (e) {
        if (e.code === 'permission_denied') throw err('not_granted', 'Metabase pulls need edit access.');
        throw err(e.code === 'unavailable' ? 'unavailable' : 'query_failed', 'Metabase: ' + e.message);
      }
    },
  };

  const caps = { db, user, downloads, mcp, assets };
  window.claude = {
    async use(name) {
      if (!caps[name]) throw err('not_granted', name + ' is not available here.');
      /* Metabase runs with the app's key, so only editors get it; viewers see the last saved pull. */
      if (name === 'mcp' && !(await me()).can_write) throw err('not_granted', 'Metabase pulls need edit access.');
      return caps[name];
    },
  };
})();
