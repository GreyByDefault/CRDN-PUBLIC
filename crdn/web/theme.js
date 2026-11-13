"use strict";
(() => {
  window.crdnApplyTheme = value => {
    const theme = value === 'light' ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('crdn-theme', theme); } catch {}
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme === 'light' ? '#f4f6f7' : '#171a1d';
  };
  let theme = 'dark';
  try { theme = localStorage.getItem('crdn-theme') || 'dark'; } catch {}
  window.crdnApplyTheme(theme);
})();
