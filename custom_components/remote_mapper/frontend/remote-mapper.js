/*
 * Remote Mapper frontend v1.0.0
 * Vanilla Web Components (Custom Elements v1 + Shadow DOM), ES2020, zero dependencies.
 * Talks to the remote_mapper integration over the Home Assistant websocket API.
 *
 * Elements:
 *   <remote-mapper-panel>        sidebar panel (registered by the integration)
 *   <remote-mapper-card>         Lovelace card: type: custom:remote-mapper-card  [entry_id: <optional>]
 *   <remote-mapper-card-editor>  visual card editor
 *   <remote-mapper-view>         the editor UI used by both
 */
(() => {
  const VERSION = '1.0.0';
  const WS_TIMEOUT_MS = 15000;
  const MAX_LIST = 150;
  const SWITCHABLE = ['light', 'switch', 'fan', 'input_boolean', 'media_player', 'cover', 'climate', 'humidifier', 'lock', 'valve', 'siren', 'automation', 'group'];

  // Must match actions.py KINDS.
  const KINDS = {
    toggle: { label: 'Toggle', icon: 'mdi:toggle-switch-outline', domains: SWITCHABLE },
    on: { label: 'Turn on', icon: 'mdi:power', domains: SWITCHABLE },
    off: { label: 'Turn off', icon: 'mdi:power-off', domains: SWITCHABLE },
    brighten: { label: 'Brighten', icon: 'mdi:brightness-7', domains: ['light'], step: true },
    dim: { label: 'Dim', icon: 'mdi:brightness-4', domains: ['light'], step: true },
    set_brightness: { label: 'Set brightness', icon: 'mdi:brightness-percent', domains: ['light'], bri: true },
    scene: { label: 'Scene', icon: 'mdi:palette-outline', domains: ['scene'] },
    script: { label: 'Script', icon: 'mdi:script-text-outline', domains: ['script'] },
    play_pause: { label: 'Play / pause', icon: 'mdi:play-pause', domains: ['media_player'] },
    volume_up: { label: 'Volume up', icon: 'mdi:volume-plus', domains: ['media_player'] },
    volume_down: { label: 'Volume down', icon: 'mdi:volume-minus', domains: ['media_player'] },
    custom: { label: 'Custom action', icon: 'mdi:code-braces', domains: null, custom: true },
    none: { label: 'Nothing', icon: 'mdi:close-circle-outline', domains: [] },
  };

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const clone = (o) => JSON.parse(JSON.stringify(o ?? {}));
  const deepEq = (a, b) => JSON.stringify(sortDeep(a)) === JSON.stringify(sortDeep(b));
  function sortDeep(v) {
    if (Array.isArray(v)) return v.map(sortDeep);
    if (v && typeof v === 'object') return Object.keys(v).sort().reduce((o, k) => { o[k] = sortDeep(v[k]); return o; }, {});
    return v;
  }
  const errMsg = (e) => e?.message || e?.error?.message || e?.code || String(e);
  const withTimeout = (p, what) =>
    Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error(`${what} timed out`)), WS_TIMEOUT_MS))]);
  const ls = {
    get(k) { try { return window.localStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch (_) { /* storage unavailable */ } },
  };
  const ws = (hass, msg, what) => withTimeout(hass.callWS(msg), what || msg.type);

  // ======================================================================= view
  class RemoteMapperView extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this._entryId = null;
      this._data = null;        // {layout, map, edit_mode, title, battery_entity, ...}
      this._draft = null;
      this._editing = false;
      this._modal = null;
      this._busy = false;
      this._error = '';
      this._unsub = null;
      this._subFor = null;
      this._flash = '';
      this._discardArmed = false;
      this._sig = '';
      this._selfSave = false;
      this._onClick = this._onClick.bind(this);
      this._onInput = this._onInput.bind(this);
      this._onKey = this._onKey.bind(this);
      this.shadowRoot.innerHTML = `<style>${STYLE}</style><div class="wrap"><div id="main"></div><div class="toast"></div></div><div id="modal"></div>`;
    }

    set noRemotes(v) { this._noRemotes = !!v; this._render(); }

    set entryId(v) {
      if (v === this._entryId) return;
      this._teardown();
      this._entryId = v || null;
      this._data = null; this._draft = null; this._editing = false; this._modal = null; this._error = '';
      this._renderModal();
      this._maybeInit();
      this._render();
    }
    get entryId() { return this._entryId; }

    set hass(h) {
      const first = !this._hass;
      this._hass = h;
      if (first) this._maybeInit();
      const bat = this._data?.battery_entity ? h?.states?.[this._data.battery_entity]?.state : '';
      const sig = `${bat}|${h?.user?.is_admin}`;
      if (sig !== this._sig) { this._sig = sig; this._render(); }
    }
    get hass() { return this._hass; }

    connectedCallback() {
      this.shadowRoot.addEventListener('click', this._onClick);
      this.shadowRoot.addEventListener('input', this._onInput);
      window.addEventListener('keydown', this._onKey);
      this._maybeInit();
    }

    disconnectedCallback() {
      this.shadowRoot.removeEventListener('click', this._onClick);
      this.shadowRoot.removeEventListener('input', this._onInput);
      window.removeEventListener('keydown', this._onKey);
      this._teardown();
    }

    _teardown() {
      const u = this._unsub; this._unsub = null; this._subFor = null;
      if (u) Promise.resolve(u).then((fn) => { try { if (typeof fn === 'function') fn(); } catch (_) { /* already closed */ } }).catch(() => {});
    }

    async _maybeInit() {
      if (!this._hass || !this._entryId || !this.isConnected || this._subFor === this._entryId) return;
      const id = this._entryId;
      this._subFor = id;
      try {
        this._unsub = this._hass.connection.subscribeMessage((m) => this._onMessage(id, m), { type: 'remote_mapper/subscribe', entry_id: id });
        await this._unsub;
        const data = await ws(this._hass, { type: 'remote_mapper/get', entry_id: id }, 'Loading remote');
        if (id !== this._entryId) return;
        this._data = data;
        this._error = '';
        if (data.edit_mode && !this._editing) this._startEditingLocal();
      } catch (e) {
        if (id !== this._entryId) return;
        this._subFor = null;
        this._error = `Couldn't load this remote: ${errMsg(e)}`;
      }
      this._render();
    }

    _onMessage(id, m) {
      if (id !== this._entryId || !m || !this._data) return;
      if (m.type === 'press') {
        if (this._editing && !this._modal && !this._busy) this._openModal(m.slot);
      } else if (m.type === 'edit_mode') {
        this._data.edit_mode = !!m.enabled;
        if (m.enabled && !this._editing) { this._startEditingLocal(); this._render(); }
        else if (!m.enabled && this._editing && !this._busy) {
          if (this._dirty()) this._save(true).then((ok) => ok && this._toast('Edit mode timed out — changes saved'));
          else { this._stopEditingLocal(); this._toast(m.reason === 'timeout' ? 'Edit mode timed out' : 'Edit mode off'); }
        }
      } else if (m.type === 'saved') {
        this._data.map = m.map || {};
        if (!this._editing) this._render();
      }
    }

    // ---------------------------------------------------------- editing
    _dirty() { return this._editing && !deepEq(this._draft || {}, this._data?.map || {}); }

    _startEditingLocal() {
      this._draft = clone(this._data?.map || {});
      this._editing = true;
      this._discardArmed = false;
    }

    _stopEditingLocal() {
      this._editing = false; this._draft = null; this._modal = null; this._flash = ''; this._discardArmed = false;
      this._renderModal(); this._render();
    }

    async _enterEdit() {
      if (this._busy || this._editing || !this._data) return;
      this._busy = true; this._render();
      try {
        this._data = await ws(this._hass, { type: 'remote_mapper/get', entry_id: this._entryId });
        this._startEditingLocal();
        await ws(this._hass, { type: 'remote_mapper/edit_mode', entry_id: this._entryId, enabled: true });
        this._data.edit_mode = true;
      } catch (e) {
        this._editing = false; this._draft = null;
        this._toast(`Couldn't start editing: ${errMsg(e)}`, true);
      }
      this._busy = false; this._render();
    }

    async _leaveEdit() {
      this._stopEditingLocal();
      try { await ws(this._hass, { type: 'remote_mapper/edit_mode', entry_id: this._entryId, enabled: false }); }
      catch (e) { this._toast(`Couldn't resume the remote: ${errMsg(e)}`, true); }
    }

    async _save(fromTimeout = false) {
      if (this._busy) return false;
      this._busy = true; this._render();
      try {
        const res = await ws(this._hass, { type: 'remote_mapper/save', entry_id: this._entryId, map: this._draft || {} }, 'Saving');
        this._data.map = res.map || {};
        this._busy = false;
        this._stopEditingLocal();
        if (!fromTimeout) {
          await ws(this._hass, { type: 'remote_mapper/edit_mode', entry_id: this._entryId, enabled: false }).catch(() => {});
          this._toast('Saved — remote is live');
        }
        return true;
      } catch (e) {
        this._busy = false;
        this._toast(`Save failed: ${errMsg(e)}`, true);
        this._render();
        return false;
      }
    }

    _openModal(slotKey) {
      const slot = this._slot(slotKey);
      if (!slot) return;
      const cur = (this._draft || {})[slotKey];
      const defKind = cur?.kind || (slot.key.endsWith('_up') ? 'brighten' : slot.key.endsWith('_down') ? 'dim' : 'toggle');
      this._modal = {
        key: slotKey, slot, group: this._groupOf(slotKey),
        kind: KINDS[defKind] ? defKind : 'toggle',
        ents: new Set(cur?.entity_id || []),
        step: cur?.step || (slot.scroll ? 6 : 20),
        bri: cur?.brightness_pct || 50,
        service: cur?.service || '',
        dataText: cur?.data && Object.keys(cur.data).length ? JSON.stringify(cur.data, null, 2) : '',
        q: '',
      };
      this._flash = slotKey;
      ws(this._hass, { type: 'remote_mapper/edit_mode', entry_id: this._entryId, enabled: true }).catch(() => {}); // keep-alive
      this._render();
      this._renderModal();
    }

    _specFromModal() {
      const m = this._modal;
      if (!m) return { error: 'No slot selected' };
      if (m.kind === 'none') return { spec: { kind: 'none' } };
      const k = KINDS[m.kind];
      const ents = [...m.ents];
      const spec = { kind: m.kind, entity_id: ents };
      if (k.step) spec.step = m.step;
      if (k.bri) spec.brightness_pct = m.bri;
      if (k.custom) {
        const svc = (m.service || '').trim();
        if (!/^[a-z0-9_]+\.[a-z0-9_]+$/.test(svc)) return { error: 'Enter an action like light.turn_on' };
        spec.service = svc;
        if (m.dataText.trim()) {
          try {
            const d = JSON.parse(m.dataText);
            if (!d || typeof d !== 'object' || Array.isArray(d)) return { error: 'Data must be a JSON object' };
            spec.data = d;
          } catch (e) { return { error: `Data is not valid JSON: ${e.message}` }; }
        }
      } else if (!ents.length) return { error: 'Pick at least one device' };
      const names = ents.map((x) => this._name(x));
      const base = k.custom ? spec.service : k.label;
      spec.label = `${base}${names.length ? ` · ${names[0]}` : ''}${names.length > 1 ? ` +${names.length - 1}` : ''}`.replace(/[{}]/g, '').slice(0, 120);
      return { spec };
    }

    _commitModal() {
      const m = this._modal;
      if (!m || !this._draft) return;
      const { spec, error } = this._specFromModal();
      if (error) { this._toast(error, true); return; }
      if (spec.kind === 'none') delete this._draft[m.key]; else this._draft[m.key] = spec;
      this._modal = null;
      this._renderModal(); this._render();
    }

    async _test() {
      const { spec, error } = this._specFromModal();
      if (error) { this._toast(error, true); return; }
      if (spec.kind === 'none') return;
      try {
        await ws(this._hass, { type: 'remote_mapper/test', entry_id: this._entryId, spec }, 'Test');
        this._toast('Test sent');
      } catch (e) { this._toast(`Test failed: ${errMsg(e)}`, true); }
    }

    // ---------------------------------------------------------- events
    _onKey(e) { if (e.key === 'Escape' && this._modal) { this._modal = null; this._renderModal(); } }

    _onInput(e) {
      const t = e.composedPath()[0];
      const m = this._modal;
      if (!m || !t?.dataset?.f) return;
      const f = t.dataset.f;
      if (f === 'q') { m.q = t.value || ''; this._renderList(); }
      else if (f === 'service') m.service = t.value || '';
      else if (f === 'data') m.dataText = t.value || '';
      else if (f === 'step' || f === 'bri') {
        const v = Math.min(100, Math.max(1, parseInt(t.value, 10) || 1));
        if (f === 'step') m.step = v; else m.bri = v;
        const out = this.shadowRoot.querySelector(`[data-out="${f}"]`);
        if (out) out.textContent = `${v}%`;
      }
    }

    _onClick(e) {
      const path = e.composedPath();
      const el = path.find((n) => n?.dataset?.act);
      if (!el) return;
      const act = el.dataset.act;
      const m = this._modal;
      switch (act) {
        case 'edit': this._enterEdit(); break;
        case 'done': if (this._dirty()) this._save(); else { this._leaveEdit(); this._toast('No changes'); } break;
        case 'cancel':
          if (this._dirty() && !this._discardArmed) {
            this._discardArmed = true; this._render();
            setTimeout(() => { if (this._discardArmed) { this._discardArmed = false; this._render(); } }, 3000);
          } else this._leaveEdit();
          break;
        case 'retry': this._subFor = null; this._error = ''; this._maybeInit(); this._render(); break;
        case 'slot': if (this._editing && !this._busy) this._openModal(el.dataset.key); break;
        case 'scrim': if (path[0] === el) { this._modal = null; this._renderModal(); } break;
        case 'm-close': this._modal = null; this._renderModal(); break;
        case 'kind':
          if (!m) break;
          m.kind = el.dataset.kind;
          { const doms = KINDS[m.kind].domains; if (doms) m.ents = new Set([...m.ents].filter((x) => doms.includes(x.split('.')[0]))); }
          this._renderModal();
          break;
        case 'ent':
          if (!m) break;
          if (m.ents.has(el.dataset.eid)) m.ents.delete(el.dataset.eid); else m.ents.add(el.dataset.eid);
          this._renderSelected(); this._renderList(); this._renderFooter();
          break;
        case 'm-test': this._test(); break;
        case 'm-set': this._commitModal(); break;
        default: break;
      }
    }

    // ---------------------------------------------------------- helpers
    _slot(key) { for (const g of this._data?.layout?.groups || []) for (const s of g.slots) if (s.key === key) return s; return null; }
    _groupOf(key) { return (this._data?.layout?.groups || []).find((g) => g.slots.some((s) => s.key === key)) || null; }
    _name(eid) { return this._hass?.states?.[eid]?.attributes?.friendly_name || eid; }
    _areaOf(eid) {
      const reg = this._hass?.entities?.[eid];
      if (!reg) return null;
      if (reg.area_id) return reg.area_id;
      return reg.device_id ? this._hass?.devices?.[reg.device_id]?.area_id || null : null;
    }
    _remoteArea() {
      const dev = this._data?.device_id ? this._hass?.devices?.[this._data.device_id] : null;
      return dev?.area_id || null;
    }
    _toast(msg, err = false) {
      const t = this.shadowRoot.querySelector('.toast');
      if (!t) return;
      t.textContent = msg; t.classList.toggle('err', !!err); t.classList.add('show');
      clearTimeout(this._toastT);
      this._toastT = setTimeout(() => t.classList.remove('show'), err ? 5000 : 2600);
    }
    _specHtml(spec) {
      const k = KINDS[spec.kind] || { label: spec.kind, icon: 'mdi:help-circle-outline' };
      const ents = Array.isArray(spec.entity_id) ? spec.entity_id : [];
      const names = ents.map((x) => this._name(x));
      const extra = k.step ? ` ${spec.step}%` : k.bri ? ` ${spec.brightness_pct}%` : '';
      const top = k.custom ? spec.service : (names[0] || '?');
      const more = !k.custom && names.length > 1 ? ` <span class="more">+${names.length - 1}</span>` : '';
      const sub = k.custom ? (names.length ? `${names[0]}${names.length > 1 ? ` +${names.length - 1}` : ''}` : 'Custom action') : k.label + extra;
      return `<ha-icon class="ki" icon="${k.icon}"></ha-icon><div class="rt"><div class="rd">${esc(top)}${more}</div><div class="rk">${esc(sub)}</div></div>`;
    }

    // ---------------------------------------------------------- render
    _render() {
      const root = this.shadowRoot.getElementById('main');
      if (!root) return;
      const h = this._hass;
      if (!this._entryId) { root.innerHTML = this._noRemotes ? `<div class="empty">No remotes set up yet. Add one under Settings → Devices &amp; services → Add integration → Remote Mapper.</div>` : `<div class="empty"><span class="spin"></span> Loading…</div>`; return; }
      if (this._error) { root.innerHTML = `<div class="banner err">${esc(this._error)} <button class="btn sm" data-act="retry">Retry</button></div>`; return; }
      if (!this._data) { root.innerHTML = `<div class="empty"><span class="spin"></span> Loading…</div>`; return; }
      const admin = !!h?.user?.is_admin;
      const map = this._editing ? (this._draft || {}) : (this._data.map || {});
      const bat = this._data.battery_entity ? h?.states?.[this._data.battery_entity]?.state : null;
      const batN = parseFloat(bat);
      const sub = [
        Number.isFinite(batN) ? `<span class="${batN < 20 ? 'warn' : ''}">Battery ${Math.round(batN)}%</span>` : '',
        this._editing ? '<span class="live">Editing · remote paused</span>' : (admin ? '<span>Tap the pencil to change buttons</span>' : '<span>Read only — ask an admin to change buttons</span>'),
      ].filter(Boolean).join(' · ');

      const groups = (this._data.layout?.groups || []).map((g) => `
        <div class="ch">
          <div class="chh">${esc(g.label)}</div>
          ${g.slots.map((s) => {
            const spec = map[s.key];
            const changed = this._editing && !deepEq(spec || null, this._data.map?.[s.key] || null);
            return `<div class="row ${this._editing ? 'editable' : ''} ${this._flash === s.key ? 'flash' : ''}" ${this._editing ? `data-act="slot" data-key="${esc(s.key)}"` : ''}>
                <ha-icon icon="${esc(s.icon || 'mdi:gesture-tap')}"></ha-icon>
                <div class="rl">${esc(s.label)}</div>
                <div class="rv ${spec ? '' : 'empty-v'}">${spec ? this._specHtml(spec) : '—'}</div>
                ${changed ? '<span class="dot" title="Unsaved"></span>' : '<span></span>'}
              </div>`;
          }).join('')}
        </div>`).join('');

      root.innerHTML = `
        <div class="hdr">
          <div class="ttl">
            <div class="badge"><ha-icon icon="mdi:remote"></ha-icon></div>
            <div><div class="name">${esc(this._data.title)}</div><div class="sub">${sub}</div></div>
          </div>
          ${!this._editing && admin ? `<button class="icon-btn" data-act="edit" title="Edit buttons" ${this._busy ? 'disabled' : ''}><ha-icon icon="mdi:pencil"></ha-icon></button>` : ''}
        </div>
        ${this._editing ? `<div class="banner live"><span class="pulse"></span><div><b>Listening.</b> Press, scroll or hold a button on the remote — or tap a slot below.</div></div>` : ''}
        ${groups ? `<div class="chs">${groups}</div>` : '<div class="empty">This remote has no buttons Remote Mapper can use.</div>'}
        ${this._editing ? `
          <div class="foot">
            <button class="btn ${this._discardArmed ? 'danger' : ''}" data-act="cancel" ${this._busy ? 'disabled' : ''}>${this._discardArmed ? 'Discard changes?' : 'Cancel'}</button>
            <button class="btn primary" data-act="done" ${this._busy ? 'disabled' : ''}>${this._busy ? 'Saving…' : 'Done'}</button>
          </div>` : ''}`;
    }

    _renderModal() {
      const host = this.shadowRoot.getElementById('modal');
      if (!host) return;
      const m = this._modal;
      if (!m) { host.innerHTML = ''; return; }
      const k = KINDS[m.kind];
      const services = Object.entries(this._hass?.services || {}).flatMap(([d, s]) => Object.keys(s || {}).map((n) => `${d}.${n}`)).sort();
      host.innerHTML = `
        <div class="scrim" data-act="scrim">
          <div class="sheet" role="dialog" aria-modal="true">
            <div class="mh">
              <div>
                <div class="mt"><ha-icon icon="${esc(m.slot.icon || 'mdi:gesture-tap')}"></ha-icon> ${esc(m.group?.label ? `${m.group.label} · ` : '')}${esc(m.slot.label)}</div>
                <div class="ms">What do you want this button to do?</div>
              </div>
              <button class="icon-btn" data-act="m-close" title="Close"><ha-icon icon="mdi:close"></ha-icon></button>
            </div>
            <div class="kinds">
              ${Object.entries(KINDS).map(([id, d]) => `<button class="kind ${m.kind === id ? 'on' : ''}" data-act="kind" data-kind="${id}"><ha-icon icon="${d.icon}"></ha-icon><span>${d.label}</span></button>`).join('')}
            </div>
            ${k.step ? `<label class="opt">Step per ${m.slot.scroll ? 'click of the wheel' : 'press'}<input type="range" min="1" max="50" value="${m.step}" data-f="step"><b data-out="step">${m.step}%</b></label>` : ''}
            ${k.bri ? `<label class="opt">Brightness<input type="range" min="1" max="100" value="${m.bri}" data-f="bri"><b data-out="bri">${m.bri}%</b></label>` : ''}
            ${k.custom ? `
              <label class="fld">Action<input data-f="service" list="rm-services" placeholder="e.g. light.turn_on" value="${esc(m.service)}" autocomplete="off" spellcheck="false"></label>
              <datalist id="rm-services">${services.map((s) => `<option value="${esc(s)}">`).join('')}</datalist>
              <label class="fld">Data (JSON, optional)<textarea data-f="data" rows="3" placeholder='{"brightness_pct": 40, "color_name": "purple"}' spellcheck="false">${esc(m.dataText)}</textarea></label>` : ''}
            ${m.kind === 'none' ? `<div class="none">This button will do nothing.</div>` : `
              <div class="picker">
                <div class="search"><ha-icon icon="mdi:magnify"></ha-icon><input data-f="q" placeholder="Search devices${k.custom ? ' (optional target)' : ''}…" value="${esc(m.q)}" autocomplete="off"></div>
                <div class="sel"></div>
                <div class="list"></div>
              </div>`}
            <div class="mf"></div>
          </div>
        </div>`;
      this._renderSelected(); this._renderList(); this._renderFooter();
    }

    _renderFooter() {
      const f = this.shadowRoot.querySelector('.mf');
      const m = this._modal;
      if (!f || !m) return;
      const k = KINDS[m.kind];
      const can = m.kind === 'none' || k.custom || m.ents.size > 0;
      f.innerHTML = `
        ${m.kind !== 'none' ? `<button class="btn" data-act="m-test" ${can ? '' : 'disabled'}><ha-icon icon="mdi:play-circle-outline"></ha-icon> Test</button>` : ''}
        <span class="sp"></span>
        <button class="btn" data-act="m-close">Cancel</button>
        <button class="btn primary" data-act="m-set" ${can ? '' : 'disabled'}>Set</button>`;
    }

    _renderSelected() {
      const el = this.shadowRoot.querySelector('.sel');
      const m = this._modal;
      if (!el || !m) return;
      el.innerHTML = m.ents.size
        ? [...m.ents].map((x) => `<button class="chip" data-act="ent" data-eid="${esc(x)}">${esc(this._name(x))}<ha-icon icon="mdi:close"></ha-icon></button>`).join('')
        : '<span class="hint">Nothing selected yet</span>';
    }

    _renderList() {
      const el = this.shadowRoot.querySelector('.list');
      const m = this._modal;
      const h = this._hass;
      if (!el || !m || !h) return;
      const doms = KINDS[m.kind].domains;
      const q = m.q.trim().toLowerCase();
      const area = this._remoteArea();
      const own = new Set(Object.values(h.entities || {}).filter((e) => e.device_id && e.device_id === this._data?.device_id).map((e) => e.entity_id));
      const rows = Object.keys(h.states)
        .filter((eid) => (!doms || doms.includes(eid.split('.')[0])) && !own.has(eid))
        .filter((eid) => !h.entities?.[eid]?.hidden && !h.entities?.[eid]?.entity_category)
        .map((eid) => ({ eid, name: this._name(eid), here: !!area && this._areaOf(eid) === area }))
        .filter((r) => !q || r.name.toLowerCase().includes(q) || r.eid.includes(q))
        .sort((a, b) => (b.here - a.here) || a.name.localeCompare(b.name));
      const shown = rows.slice(0, MAX_LIST);
      el.innerHTML = shown.length
        ? shown.map((r) => `
            <button class="ent ${m.ents.has(r.eid) ? 'on' : ''}" data-act="ent" data-eid="${esc(r.eid)}">
              <ha-icon icon="${m.ents.has(r.eid) ? 'mdi:checkbox-marked-circle' : 'mdi:checkbox-blank-circle-outline'}"></ha-icon>
              <span class="en">${esc(r.name)}${r.here ? ' <em>same room</em>' : ''}</span>
              <span class="ei">${esc(r.eid)}</span>
            </button>`).join('') + (rows.length > MAX_LIST ? `<div class="hint pad">${rows.length - MAX_LIST} more — refine the search</div>` : '')
        : '<div class="hint pad">No matches</div>';
    }
  }

  // ======================================================================= picker helper
  async function listRemotes(hass) {
    const res = await ws(hass, { type: 'remote_mapper/list' }, 'Listing remotes');
    return Array.isArray(res?.remotes) ? res.remotes : [];
  }

  // ======================================================================= panel
  class RemoteMapperPanel extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this._remotes = null;
      this._sel = ls.get('remote_mapper.selected');
      this._error = '';
      this.shadowRoot.innerHTML = `<style>${STYLE}${PANEL_STYLE}</style>
        <div class="toolbar"><span id="menu"></span><div class="tt">Remote Mapper</div>
          <a class="icon-btn" href="/config/integrations/dashboard/add?domain=remote_mapper" title="Add a remote"><ha-icon icon="mdi:plus"></ha-icon></a></div>
        <div class="content"><div id="tabs"></div><div class="card"><remote-mapper-view></remote-mapper-view></div><div id="state"></div></div>`;
      this.shadowRoot.addEventListener('click', (e) => {
        const t = e.composedPath().find((n) => n?.dataset?.remote);
        if (t) { this._sel = t.dataset.remote; ls.set('remote_mapper.selected', this._sel); this._renderTabs(); }
      });
    }
    set hass(h) {
      this._hass = h;
      this._view().hass = h;
      const mb = this._menu();
      if (mb) { mb.hass = h; mb.narrow = this._narrow; }
      if (this._remotes === null && !this._loading) this._load();
    }
    set narrow(n) { this._narrow = n; const mb = this._menu(); if (mb) mb.narrow = n; }
    set panel(_p) { /* unused */ }
    set route(_r) { /* unused */ }
    _view() { return this.shadowRoot.querySelector('remote-mapper-view'); }
    _menu() {
      const slot = this.shadowRoot.getElementById('menu');
      if (!slot) return null;
      if (!slot.firstChild && customElements.get('ha-menu-button')) slot.appendChild(document.createElement('ha-menu-button'));
      return slot.firstChild;
    }
    async _load() {
      this._loading = true;
      try { this._remotes = await listRemotes(this._hass); this._error = ''; }
      catch (e) { this._remotes = []; this._error = errMsg(e); }
      this._loading = false;
      this._renderTabs();
    }
    _renderTabs() {
      const tabs = this.shadowRoot.getElementById('tabs');
      const state = this.shadowRoot.getElementById('state');
      const card = this.shadowRoot.querySelector('.card');
      const list = this._remotes || [];
      const loaded = list.filter((r) => r.loaded);
      if (!loaded.find((r) => r.entry_id === this._sel)) this._sel = loaded[0]?.entry_id || null;
      tabs.innerHTML = list.length > 1 ? list.map((r) => `<button class="tab ${r.entry_id === this._sel ? 'on' : ''}" data-remote="${esc(r.entry_id)}" ${r.loaded ? '' : 'disabled'}>${esc(r.title)}</button>`).join('') : '';
      const bad = list.filter((r) => !r.loaded);
      state.innerHTML = [
        this._error ? `<div class="banner err">Couldn't list remotes: ${esc(this._error)}</div>` : '',
        !list.length && !this._error ? `<div class="empty big"><ha-icon icon="mdi:remote"></ha-icon><div>No remotes yet.</div><a class="btn primary" href="/config/integrations/dashboard/add?domain=remote_mapper">Add a remote</a></div>` : '',
        bad.length ? `<div class="banner">${bad.map((r) => `${esc(r.title)}: ${esc(r.state)}`).join(' · ')} — see Settings → Devices &amp; services.</div>` : '',
      ].join('');
      card.style.display = this._sel ? '' : 'none';
      this._view().entryId = this._sel;
    }
  }

  // ======================================================================= card
  class RemoteMapperCard extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.innerHTML = `<style>${STYLE}</style><ha-card><div id="tabs" class="tabs-in"></div><remote-mapper-view></remote-mapper-view></ha-card>`;
      this._remotes = null;
      this.shadowRoot.addEventListener('click', (e) => {
        const t = e.composedPath().find((n) => n?.dataset?.remote);
        if (t) { this._sel = t.dataset.remote; this._apply(); }
      });
    }
    static getConfigElement() { return document.createElement('remote-mapper-card-editor'); }
    static getStubConfig() { return {}; }
    setConfig(config) {
      if (config && typeof config !== 'object') throw new Error('remote-mapper-card: invalid config');
      this._config = { ...(config || {}) };
      this._sel = this._config.entry_id || this._sel || null;
      this._apply();
    }
    set hass(h) {
      this._hass = h;
      this.shadowRoot.querySelector('remote-mapper-view').hass = h;
      if (this._remotes === null && !this._loading) this._load();
    }
    getCardSize() { return 8; }
    getGridOptions() { return { columns: 12, min_columns: 6 }; }
    async _load() {
      this._loading = true;
      try { this._remotes = (await listRemotes(this._hass)).filter((r) => r.loaded); } catch (_) { this._remotes = []; }
      this._loading = false;
      this._apply();
    }
    _apply() {
      const view = this.shadowRoot.querySelector('remote-mapper-view');
      const tabs = this.shadowRoot.getElementById('tabs');
      const list = this._remotes || [];
      if (this._config?.entry_id) { tabs.innerHTML = ''; view.entryId = this._config.entry_id; return; }
      if (!this._sel || (list.length && !list.find((r) => r.entry_id === this._sel))) this._sel = list[0]?.entry_id || null;
      view.noRemotes = this._remotes !== null && !list.length;
      tabs.innerHTML = list.length > 1 ? list.map((r) => `<button class="tab ${r.entry_id === this._sel ? 'on' : ''}" data-remote="${esc(r.entry_id)}">${esc(r.title)}</button>`).join('') : '';
      view.entryId = this._sel;
    }
  }

  class RemoteMapperCardEditor extends HTMLElement {
    setConfig(config) { this._config = { ...(config || {}) }; this._render(); }
    set hass(h) { const first = !this._hass; this._hass = h; if (first) this._load(); }
    async _load() {
      try { this._remotes = await listRemotes(this._hass); } catch (_) { this._remotes = []; }
      this._render();
    }
    _render() {
      if (!this._config) return;
      const list = this._remotes || [];
      this.innerHTML = `<div style="display:flex;flex-direction:column;gap:6px;padding:8px 0">
        <label style="font-weight:500">Remote</label>
        <select style="padding:8px;border-radius:6px;font:inherit">
          <option value="">All remotes (tabs)</option>
          ${list.map((r) => `<option value="${esc(r.entry_id)}" ${r.entry_id === this._config.entry_id ? 'selected' : ''}>${esc(r.title)}</option>`).join('')}
        </select></div>`;
      this.querySelector('select').addEventListener('change', (e) => {
        const cfg = { ...this._config };
        if (e.target.value) cfg.entry_id = e.target.value; else delete cfg.entry_id;
        this._config = cfg;
        this.dispatchEvent(new CustomEvent('config-changed', { detail: { config: cfg }, bubbles: true, composed: true }));
      });
    }
  }

  // ======================================================================= styles
  const STYLE = `
    :host { display:block; --rm-accent: var(--primary-color, #03a9f4); --rm-r: var(--ha-card-border-radius, 12px); color: var(--primary-text-color); }
    ha-card { padding: 16px; position: relative; overflow: hidden; }
    .wrap { position: relative; }
    button { font: inherit; color: inherit; cursor: pointer; }
    button[disabled] { opacity: .45; cursor: default; }
    a { color: inherit; }
    .hdr { display:flex; align-items:center; justify-content:space-between; gap:12px; }
    .ttl { display:flex; align-items:center; gap:12px; min-width:0; }
    .badge { width:42px; height:42px; border-radius:50%; display:grid; place-items:center; flex:none; background: color-mix(in srgb, var(--rm-accent) 15%, transparent); color: var(--rm-accent); }
    .name { font-size: 1.15rem; font-weight: 600; }
    .sub { font-size: .85rem; color: var(--secondary-text-color); }
    .sub .warn { color: var(--error-color, #db4437); font-weight:600; }
    .sub .live { color: var(--rm-accent); font-weight:600; }
    .icon-btn { border:none; background: color-mix(in srgb, var(--primary-text-color) 6%, transparent); width:42px; height:42px; border-radius:50%; display:grid; place-items:center; flex:none; text-decoration:none; }
    .icon-btn:hover:not([disabled]) { background: color-mix(in srgb, var(--rm-accent) 18%, transparent); color: var(--rm-accent); }
    .banner { margin-top:14px; padding:10px 12px; border-radius:10px; font-size:.9rem; display:flex; gap:10px; align-items:center; flex-wrap:wrap; background: color-mix(in srgb, var(--primary-text-color) 6%, transparent); }
    .banner.live { background: color-mix(in srgb, var(--rm-accent) 12%, transparent); }
    .banner.err { background: color-mix(in srgb, var(--error-color, #db4437) 14%, transparent); color: var(--error-color, #db4437); }
    .pulse { width:10px; height:10px; border-radius:50%; background: var(--rm-accent); flex:none; animation: pulse 1.4s infinite; }
    @keyframes pulse { 0%{box-shadow:0 0 0 0 color-mix(in srgb, var(--rm-accent) 60%, transparent)} 70%{box-shadow:0 0 0 10px transparent} 100%{box-shadow:0 0 0 0 transparent} }
    .spin { display:inline-block; width:14px; height:14px; border:2px solid var(--divider-color, #ccc); border-top-color: var(--rm-accent); border-radius:50%; animation: spin 1s linear infinite; vertical-align:-2px; margin-right:6px; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .empty { color: var(--secondary-text-color); padding: 16px; text-align:center; }
    .chs { display:grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap:12px; margin-top:14px; }
    .ch { border:1px solid var(--divider-color, rgba(127,127,127,.25)); border-radius:var(--rm-r); padding:8px; min-width:0; }
    .chh { font-weight:600; padding:4px 6px 8px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .row { display:grid; grid-template-columns: 22px 90px minmax(0,1fr) 8px; align-items:center; gap:8px; padding:8px 6px; border-radius:8px; color: var(--secondary-text-color); --mdc-icon-size:20px; min-height:24px; }
    .row.editable { cursor:pointer; }
    .row.editable:hover { background: color-mix(in srgb, var(--rm-accent) 8%, transparent); }
    .row.flash { background: color-mix(in srgb, var(--rm-accent) 18%, transparent); }
    .rl { font-size:.85rem; overflow:hidden; text-overflow:ellipsis; }
    .rv { font-size:.9rem; color: var(--primary-text-color); display:flex; align-items:center; gap:6px; min-width:0; }
    .rv.empty-v { color: var(--disabled-text-color, #999); }
    .rt { min-width:0; line-height:1.2; }
    .rd, .rk { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .rk { font-size:.75rem; color: var(--secondary-text-color); }
    .more { font-size:.75rem; color: var(--rm-accent); }
    .rv .ki { --mdc-icon-size:16px; color: var(--rm-accent); flex:none; }
    .dot { width:8px; height:8px; border-radius:50%; background: var(--warning-color, #ffa600); }
    .foot { display:flex; justify-content:flex-end; gap:10px; margin-top:16px; }
    .btn { border:1px solid var(--divider-color, rgba(127,127,127,.35)); background: transparent; padding:9px 18px; border-radius:999px; display:inline-flex; align-items:center; gap:6px; --mdc-icon-size:18px; text-decoration:none; }
    .btn.sm { padding:4px 12px; }
    .btn.primary { background: var(--rm-accent); border-color: var(--rm-accent); color: var(--text-primary-color, #fff); font-weight:600; }
    .btn.danger { border-color: var(--error-color, #db4437); color: var(--error-color, #db4437); }
    .toast { position:absolute; left:50%; bottom:0; transform: translate(-50%, 20px); opacity:0; pointer-events:none; background: var(--primary-text-color); color: var(--card-background-color, #fff); padding:8px 14px; border-radius:999px; font-size:.85rem; transition: all .25s; white-space:nowrap; max-width:90%; overflow:hidden; text-overflow:ellipsis; z-index:2; }
    .toast.show { opacity:1; transform: translate(-50%, 0); }
    .toast.err { background: var(--error-color, #db4437); color:#fff; }
    .tabs-in, #tabs { display:flex; gap:8px; flex-wrap:wrap; }
    .tabs-in:not(:empty) { margin-bottom:12px; }
    .tab { border:1px solid var(--divider-color, rgba(127,127,127,.35)); background:transparent; padding:6px 14px; border-radius:999px; font-size:.9rem; }
    .tab.on { background: var(--rm-accent); border-color: var(--rm-accent); color: var(--text-primary-color, #fff); }
    .scrim { position:fixed; inset:0; background: rgba(0,0,0,.5); z-index: 999; display:flex; align-items:center; justify-content:center; padding:16px; }
    .sheet { background: var(--card-background-color, var(--ha-card-background, #fff)); color: var(--primary-text-color); width:min(580px, 100%); max-height: calc(100vh - 32px); max-height: calc(100dvh - 32px); overflow:auto; border-radius:16px; padding:18px; box-shadow: 0 10px 40px rgba(0,0,0,.35); display:flex; flex-direction:column; gap:14px; }
    @media (max-width: 600px) { .scrim { align-items:flex-end; padding:0; } .sheet { border-radius:16px 16px 0 0; max-height: 88dvh; padding:14px; gap:12px; } .kind { padding:8px 4px; font-size:.75rem; } .list { max-height: 200px; } }
    .mh { display:flex; justify-content:space-between; align-items:flex-start; gap:10px; }
    .mt { font-size:1.1rem; font-weight:600; display:flex; align-items:center; gap:8px; }
    .ms { color: var(--secondary-text-color); font-size:.9rem; margin-top:2px; }
    .kinds { display:grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap:8px; }
    .kind { border:1px solid var(--divider-color, rgba(127,127,127,.3)); background:transparent; border-radius:12px; padding:10px 8px; display:flex; flex-direction:column; align-items:center; gap:4px; font-size:.82rem; --mdc-icon-size:22px; }
    .kind.on { border-color: var(--rm-accent); background: color-mix(in srgb, var(--rm-accent) 14%, transparent); color: var(--rm-accent); font-weight:600; }
    .opt { display:grid; grid-template-columns: auto 1fr 48px; align-items:center; gap:10px; font-size:.9rem; }
    .opt input { width:100%; accent-color: var(--rm-accent); }
    .opt b { text-align:right; }
    .fld { display:flex; flex-direction:column; gap:6px; font-size:.85rem; color: var(--secondary-text-color); }
    .fld input, .fld textarea { font: inherit; font-family: var(--code-font-family, monospace); color: var(--primary-text-color); background: transparent; border:1px solid var(--divider-color, rgba(127,127,127,.35)); border-radius:10px; padding:9px 10px; resize: vertical; }
    .none { color: var(--secondary-text-color); text-align:center; padding:12px; }
    .search { display:flex; align-items:center; gap:8px; border:1px solid var(--divider-color, rgba(127,127,127,.35)); border-radius:10px; padding:0 10px; --mdc-icon-size:20px; color: var(--secondary-text-color); }
    .search input { flex:1; border:none; outline:none; background:transparent; color: var(--primary-text-color); padding:10px 0; font: inherit; min-width:0; }
    .sel { display:flex; flex-wrap:wrap; gap:6px; margin:10px 0 6px; min-height:28px; align-items:center; }
    .chip { border:none; background: color-mix(in srgb, var(--rm-accent) 16%, transparent); color: var(--rm-accent); border-radius:999px; padding:4px 6px 4px 12px; display:inline-flex; align-items:center; gap:4px; font-size:.82rem; --mdc-icon-size:14px; }
    .list { max-height: 260px; overflow:auto; border:1px solid var(--divider-color, rgba(127,127,127,.25)); border-radius:10px; }
    .ent { width:100%; border:none; background:transparent; text-align:left; display:grid; grid-template-columns: 24px 1fr; column-gap:8px; padding:8px 10px; border-bottom:1px solid var(--divider-color, rgba(127,127,127,.15)); --mdc-icon-size:20px; color: var(--secondary-text-color); }
    .ent:last-child { border-bottom:none; }
    .ent.on { color: var(--rm-accent); background: color-mix(in srgb, var(--rm-accent) 7%, transparent); }
    .ent ha-icon { grid-row: span 2; align-self:center; }
    .en { color: var(--primary-text-color); font-size:.92rem; }
    .en em { font-style:normal; font-size:.72rem; color: var(--rm-accent); margin-left:6px; }
    .ei { font-size:.75rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .hint { color: var(--secondary-text-color); font-size:.85rem; }
    .hint.pad { padding:10px; text-align:center; }
    .mf { display:flex; align-items:center; gap:10px; }
    .sp { flex:1; }
  `;
  const PANEL_STYLE = `
    :host { background: var(--primary-background-color); min-height: 100vh; }
    .toolbar { display:flex; align-items:center; gap:8px; height:56px; padding:0 12px; background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff); position: sticky; top:0; z-index:3; }
    .toolbar .tt { flex:1; font-size:20px; font-weight:400; margin-left:8px; }
    .toolbar .icon-btn { background: transparent; color: inherit; }
    .content { max-width: 1100px; margin: 0 auto; padding: 16px; display:flex; flex-direction:column; gap:12px; }
    .card { background: var(--card-background-color, #fff); border-radius: var(--ha-card-border-radius, 12px); padding:16px; box-shadow: var(--ha-card-box-shadow, none); border: var(--ha-card-border-width, 1px) solid var(--ha-card-border-color, var(--divider-color, #e0e0e0)); }
    .empty.big { display:flex; flex-direction:column; align-items:center; gap:12px; padding:48px 16px; --mdc-icon-size:48px; }
  `;

  const define = (name, cls) => { if (!customElements.get(name)) customElements.define(name, cls); };
  define('remote-mapper-view', RemoteMapperView);
  define('remote-mapper-panel', RemoteMapperPanel);
  define('remote-mapper-card', RemoteMapperCard);
  define('remote-mapper-card-editor', RemoteMapperCardEditor);
  window.customCards = window.customCards || [];
  if (!window.customCards.some((c) => c.type === 'remote-mapper-card')) {
    window.customCards.push({ type: 'remote-mapper-card', name: 'Remote Mapper', description: 'Assign actions to remote buttons by pressing them.', preview: false });
  }
  console.info(`%c REMOTE-MAPPER %c v${VERSION} `, 'background:#03a9f4;color:#fff', 'background:#333;color:#fff');
})();
