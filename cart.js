// Crafting cart page: expands the selected items/buildings through their
// recipes down to raw materials. Uses window.GK_DATA from data.js:
//   items:   name -> {h: page href, s: [x, y] sprite offset | i: image path}
//   recipes: [product, yield, [[material, qty]...], [stations], flags]
//            flags: 1 = needs a perk, 2 = building / upgrade
//            optional 6th element: by-products [[name, qty]...] of the same craft
(function () {
  const { items, recipes } = window.GK_DATA;
  const Cart = window.GKCart;
  const $ = sel => document.querySelector(sel);
  const PERK = 1, BUILDING = 2;

  const byProduct = new Map();
  recipes.forEach((r, i) => {
    if (!byProduct.has(r[0])) byProduct.set(r[0], []);
    byProduct.get(r[0]).push(i);
  });
  const craftable = [...byProduct.keys()].sort((a, b) => a.localeCompare(b));

  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
  const fmt = n => (Math.round(n * 100) / 100).toLocaleString('en-US');

  function icon(name, size = 28) {
    const it = items[name] || {};
    if (it.s) {
      const k = size / 34;
      return `<span class="sprite item-sprite icon" style="background-position:${it.s[0] * k}px ${it.s[1] * k}px;` +
        `background-size:${1222 * k}px auto;width:${size}px;height:${size}px"></span>`;
    }
    if (it.i) return `<img class="icon" src="${esc(it.i)}" width="${size}" height="${size}" alt="">`;
    return `<span class="icon icon-none" style="width:${size}px;height:${size}px"></span>`;
  }
  function link(name) {
    const it = items[name] || {};
    return it.h ? `<a href="wiki/${esc(it.h)}">${esc(name)}</a>` : esc(name);
  }
  function recipeLabel(i) {
    const [, qty, mats, stations, flags] = recipes[i];
    const where = flags & BUILDING && !stations.length ? 'Build' : stations.join(', ') || '?';
    return `${where}: ${mats.map(([m, q]) => `${fmt(q)} ${m}`).join(' + ')} → ${fmt(qty)}` +
      (flags & PERK ? ' (perk)' : '');
  }

  // ---------- planning ----------
  function plan(state) {
    const autoRaw = new Set();
    const chosen = name => {
      if (state.raw[name] || autoRaw.has(name)) return null;
      const list = byProduct.get(name);
      if (!list) return null;
      const c = state.choice[name];
      return list.includes(c) ? c : list[0];
    };

    // Depth-first order (materials before products); cycles are cut by
    // treating the repeated item as raw.
    let order, color;
    for (let attempt = 0; attempt < 50; attempt++) {
      order = []; color = new Map();
      let cycle = null;
      const visit = name => {
        if (cycle) return;
        color.set(name, 1);
        const r = chosen(name);
        if (r != null) {
          for (const [m] of recipes[r][2]) {
            const c = color.get(m);
            if (c === 1) { cycle = state.items[m] ? name : m; return; }
            if (!c) visit(m);
            if (cycle) return;
          }
        }
        color.set(name, 2);
        order.push(name);
      };
      for (const name of Object.keys(state.items)) if (!color.has(name)) visit(name);
      if (!cycle) break;
      autoRaw.add(cycle);
    }

    const demand = new Map(Object.entries(state.items));
    const usedBy = new Map(); // material -> crafted products that consume it
    const steps = [], raw = [];
    for (let k = order.length - 1; k >= 0; k--) {
      const name = order[k], need = demand.get(name) || 0;
      if (need <= 0) continue;
      const r = chosen(name);
      if (r == null) continue;
      const [, qty, mats] = recipes[r];
      const crafts = Math.ceil(need / qty - 1e-9);
      for (const [m, q] of mats) {
        demand.set(m, (demand.get(m) || 0) + q * crafts);
        if (!usedBy.has(m)) usedBy.set(m, []);
        usedBy.get(m).push(name);
      }
      steps.push({ name, recipe: r, need, crafts, made: crafts * qty, usedBy: usedBy.get(name) || [] });
    }
    for (const name of order) {
      const need = demand.get(name) || 0;
      if (need > 0 && chosen(name) == null) raw.push({ name, need, why: state.raw[name] ? 'user' : autoRaw.has(name) ? 'cycle' : 'base' });
    }
    raw.sort((a, b) => a.name.localeCompare(b.name));
    return { steps: steps.reverse(), raw };
  }

  // ---------- rendering ----------
  function render() {
    const state = Cart.load();
    const names = Object.keys(state.items);
    $('#cart-empty').hidden = names.length > 0;
    $('#cart-result').hidden = names.length === 0;
    $('#cart-clear').hidden = names.length === 0;

    $('#cart-items').innerHTML = names.map(n => `
      <li class="cart-item">
        ${icon(n, 34)}
        <span class="name">${link(n)}</span>
        <span class="stepper">
          <button data-act="dec" data-item="${esc(n)}" aria-label="Less">−</button>
          <input type="number" inputmode="numeric" min="1" value="${state.items[n]}" data-item="${esc(n)}" aria-label="Quantity">
          <button data-act="inc" data-item="${esc(n)}" aria-label="More">+</button>
        </span>
        <button class="remove" data-act="remove" data-item="${esc(n)}" aria-label="Remove">✕</button>
      </li>`).join('');
    if (!names.length) return;

    const { steps, raw } = plan(state);
    $('#raw-list').innerHTML = raw.map(({ name, need, why }) => `
      <li>${icon(name)}<span class="qty">${fmt(need)}</span><span class="name">${link(name)}</span>
        ${why === 'user' ? `<button class="chip" data-act="craft" data-item="${esc(name)}">craft instead</button>` : ''}
        ${why === 'cycle' ? '<span class="note">recipe loops back</span>' : ''}
      </li>`).join('') || '<li class="note">Nothing — every ingredient is a craft.</li>';

    // Steps are numbered in crafting order (materials first) but shown as a tree:
    // each craft is grouped under the product it is for. A material used by
    // several products goes under the first of them to be crafted; cart items
    // are always top-level groups.
    const num = new Map(steps.map((st, k) => [st.name, k + 1]));
    const kids = new Map(), roots = [];
    for (const st of steps) {
      st.parent = state.items[st.name] ? null
        : st.usedBy.reduce((a, b) => (num.get(b) < num.get(a) ? b : a), st.usedBy[0]);
      if (st.parent == null) roots.push(st);
      else {
        if (!kids.has(st.parent)) kids.set(st.parent, []);
        kids.get(st.parent).push(st);
      }
    }
    const stepHtml = ({ name, recipe, need, crafts, made, usedBy, parent }) => {
      const [, qty, mats, stations, flags, also] = recipes[recipe];
      const options = byProduct.get(name);
      const where = flags & BUILDING && !stations.length ? 'Build' : stations.join(' / ') || 'Unknown station';
      const others = usedBy.filter(n => n !== parent);
      const sub = kids.get(name);
      return `<li class="step-group">
        <div class="step" data-n="${num.get(name)}">
        <div class="step-head">${icon(name, 34)}
          <div><b>${link(name)}</b> <span class="qty">× ${fmt(need)}</span>
          <div class="note">${esc(where)} · ${crafts} craft${crafts > 1 ? 's' : ''} × ${fmt(qty)}${made > need ? ` · ${fmt(made - need)} left over` : ''}${
            also ? ` · also gives ${also.map(([n, q]) => `${fmt(q * crafts)} ${esc(n)}`).join(', ')}` : ''}</div>${
            others.length ? `<div class="note">${parent ? 'also ' : ''}used for ${others.map(esc).join(', ')}</div>` : ''}</div>
        </div>
        <div class="mats">${mats.map(([m, q]) => `<span class="mat">${icon(m, 22)}${fmt(q * crafts)} ${esc(m)}</span>`).join('')}</div>
        <div class="step-actions">
          ${options.length > 1 ? `<select data-item="${esc(name)}" aria-label="Recipe">${options.map(i =>
            `<option value="${i}" ${i === recipe ? 'selected' : ''}>${esc(recipeLabel(i))}</option>`).join('')}</select>` : ''}
          <button class="chip" data-act="raw" data-item="${esc(name)}">I have it / buy it</button>
        </div>
        </div>${sub ? `<ol class="sub-steps">${sub.map(stepHtml).join('')}</ol>` : ''}
      </li>`;
    };
    $('#steps-list').innerHTML = roots.map(stepHtml).join('') || '<li class="note">No crafting needed.</li>';
  }

  // ---------- events ----------
  function update(fn) {
    const s = Cart.load();
    fn(s);
    Cart.save(s);
  }
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    const n = b.dataset.item;
    update(s => {
      if (b.dataset.act === 'inc') s.items[n] += 1;
      if (b.dataset.act === 'dec') s.items[n] = Math.max(1, s.items[n] - 1);
      if (b.dataset.act === 'remove') delete s.items[n];
      if (b.dataset.act === 'raw') s.raw[n] = true;
      if (b.dataset.act === 'craft') delete s.raw[n];
    });
  });
  document.addEventListener('change', e => {
    const n = e.target.dataset.item;
    if (!n) return;
    if (e.target.matches('.stepper input')) {
      const v = Math.max(1, Math.round(+e.target.value) || 1);
      update(s => { s.items[n] = v; });
    } else if (e.target.matches('select')) {
      update(s => { s.choice[n] = +e.target.value; });
    }
  });
  $('#cart-clear').addEventListener('click', () => {
    if (confirmClear()) Cart.save({ items: {}, choice: {}, raw: {} });
  });
  // Inline two-step confirm (no browser dialogs).
  let armed = false;
  function confirmClear() {
    const b = $('#cart-clear');
    if (armed) { armed = false; b.textContent = 'Clear cart'; return true; }
    armed = true; b.textContent = 'Tap again to clear';
    setTimeout(() => { armed = false; b.textContent = 'Clear cart'; }, 3000);
    return false;
  }
  $('#reset-choices').addEventListener('click', () => update(s => { s.choice = {}; s.raw = {}; }));

  // Add-item picker
  const input = $('#cart-pick'), sugg = $('#cart-suggest');
  input.addEventListener('input', () => {
    const q = input.value.trim().toLowerCase();
    if (!q) { sugg.hidden = true; return; }
    const hits = craftable.filter(n => n.toLowerCase().includes(q))
      .sort((a, b) => a.toLowerCase().indexOf(q) - b.toLowerCase().indexOf(q)).slice(0, 12);
    sugg.innerHTML = hits.map(n => `<button data-pick="${esc(n)}">${icon(n, 24)} ${esc(n)}</button>`).join('')
      || '<div class="note">No craftable item matches</div>';
    sugg.hidden = false;
  });
  sugg.addEventListener('click', e => {
    const b = e.target.closest('[data-pick]');
    if (!b) return;
    Cart.add(b.dataset.pick);
    input.value = ''; sugg.hidden = true;
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') sugg.querySelector('[data-pick]')?.click();
  });

  window.addEventListener('cart-change', render);
  window.addEventListener('storage', render);
  render();
})();
