'use strict';
(() => {
  const button = document.querySelector('#menu-button'), menu = document.querySelector('#main-menu');
  function closeMenu(focus = false) { menu.hidden = true; button.setAttribute('aria-expanded', 'false'); if (focus) button.focus(); }
  button.onclick = () => { const open = menu.hidden; menu.hidden = !open; button.setAttribute('aria-expanded', String(open)); if (open) menu.querySelector('button').focus(); };
  document.addEventListener('click', e => { if (!menu.contains(e.target) && !button.contains(e.target)) closeMenu(); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !menu.hidden) { e.preventDefault(); closeMenu(true); }
    if (menu.hidden) return;
    const items = [...menu.querySelectorAll('button')], index = items.indexOf(document.activeElement);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); items[(index + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus(); }
    if (e.key === 'Tab') closeMenu();
  });
  menu.addEventListener('click', e => { if (e.target.closest('button')) closeMenu(); });
  document.addEventListener('close', e => { if (e.target instanceof HTMLDialogElement && (menu.contains(document.activeElement) || document.activeElement === document.body)) button.focus(); }, true);
  document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => b.closest('dialog').close());
  document.querySelector('#about-button').onclick = () => document.querySelector('#about-dialog').showModal();
  window.crdnSetServerName = name => {
    document.querySelector('#server-name').textContent = name || 'Сервер';
    document.title = name ? name + ' — CRDN' : 'CRDN — Server Monitor';
    // Revalidate the stable manifest URL; OS launcher labels may update later.
    const link = document.querySelector('#pwa-manifest'); link.removeAttribute('href'); link.href = '/manifest.webmanifest';
  };
  fetch('/api/settings', {cache:'no-store'}).then(r => { if (!r.ok) throw Error(); return r.json(); }).then(v => window.crdnSetServerName(v.server_name)).catch(() => {});
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js', {updateViaCache:'none'}).catch(() => {});
})();
