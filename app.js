// Shared page script: crafting cart storage, header badge, "add to cart"
// buttons, service worker registration and the offline download control.
(function () {
  const root = document.currentScript.src.replace(/[^/]*$/, '');
  const KEY = 'gk-cart';

  const Cart = {
    load() {
      try {
        const s = JSON.parse(localStorage.getItem(KEY));
        if (s && s.items) return Object.assign({ choice: {}, raw: {} }, s);
      } catch (e) { /* storage unavailable or corrupt: start empty */ }
      return { items: {}, choice: {}, raw: {} };
    },
    save(state) {
      try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* ignore */ }
      Cart.badge(state);
      window.dispatchEvent(new CustomEvent('cart-change', { detail: state }));
    },
    add(name, qty = 1) {
      const s = Cart.load();
      s.items[name] = (s.items[name] || 0) + qty;
      Cart.save(s);
      return s;
    },
    count(state) {
      return Object.keys(state.items).length;
    },
    badge(state = Cart.load()) {
      const n = Cart.count(state);
      document.querySelectorAll('.cart-count').forEach(el => {
        el.textContent = n;
        el.hidden = !n;
      });
    },
  };
  window.GKCart = Cart;

  let toastTimer;
  function toast(text) {
    let el = document.querySelector('.toast');
    if (!el) {
      el = document.createElement('div');
      el.className = 'toast';
      document.body.appendChild(el);
    }
    el.innerHTML = text;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 2500);
  }

  document.addEventListener('click', e => {
    const btn = e.target.closest('.cart-add');
    if (!btn) return;
    const name = btn.dataset.item;
    const s = Cart.add(name);
    toast(`<b>${name.replace(/[&<>]/g, '')}</b> × ${s.items[name]} in cart · <a href="${root}cart.html">Open cart</a>`);
  });
  window.addEventListener('storage', e => { if (e.key === KEY) Cart.badge(); });
  Cart.badge();

  // ---------- PWA ----------
  if (!('serviceWorker' in navigator) || location.protocol === 'file:') {
    document.querySelectorAll('.offline-panel').forEach(p => {
      p.querySelector('.offline-status').textContent =
        'Offline install needs the site served over http(s) — open it via a web server, not as a file.';
      p.querySelector('button').hidden = true;
    });
    return;
  }
  navigator.serviceWorker.register(root + 'sw.js').catch(err => console.warn('SW registration failed', err));

  const panel = document.querySelector('.offline-panel');
  if (!panel) return;
  const status = panel.querySelector('.offline-status');
  const button = panel.querySelector('button');
  const bar = panel.querySelector('progress');

  function render(msg) {
    if (msg.type === 'offline-status') {
      const { cached, total } = msg;
      bar.max = total; bar.value = cached; bar.hidden = false;
      const full = total && cached >= total;
      status.textContent = full
        ? `Everything is available offline (${total} files).`
        : `${cached} of ${total} files saved for offline use.`;
      button.textContent = full ? 'Re-check for updates' : cached ? 'Continue download' : 'Download everything for offline';
      button.disabled = false;
    } else if (msg.type === 'offline-progress') {
      bar.max = msg.total; bar.value = msg.done; bar.hidden = false;
      status.textContent = `Downloading… ${msg.done} / ${msg.total}` + (msg.failed ? ` (${msg.failed} failed)` : '');
      button.disabled = true;
    } else if (msg.type === 'offline-done') {
      status.textContent = msg.failed
        ? `Done, but ${msg.failed} files failed — press the button to retry.`
        : 'Everything is available offline.';
      button.disabled = false;
      button.textContent = msg.failed ? 'Retry' : 'Re-check for updates';
    }
  }
  navigator.serviceWorker.addEventListener('message', e => render(e.data));
  navigator.serviceWorker.ready.then(reg => {
    reg.active.postMessage({ type: 'offline-status' });
    button.addEventListener('click', () => {
      button.disabled = true;
      reg.active.postMessage({ type: 'offline-download', refresh: button.textContent.startsWith('Re-check') });
    });
  });
})();
