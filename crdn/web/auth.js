'use strict';
(async () => {
  let state;
  async function loadSession() {
    const response = await fetch('/api/auth/session', {cache: 'no-store'});
    if (!response.ok) throw Error('Не удалось установить сессию. Обновите страницу.');
    state = await response.json();
    return state;
  }
  async function post(operation, data = {}, signal) {
    const response = await fetch(`/api/auth/${operation}`, {method: 'POST', credentials: 'same-origin', signal,
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf_token}, body: JSON.stringify(data)});
    const value = await response.json();
    if (!response.ok) {
      const error = Error(value.error || 'Не удалось выполнить запрос.');
      error.status = response.status; throw error;
    }
    if (value.csrf_token) state = value;
    return value;
  }
  function showError(node, error) { node.textContent = error.message || 'Нет связи с сервером. Повторите попытку.'; }
  function bindPasswordToggle(button) {
    const input = document.getElementById(button.getAttribute('aria-controls'));
    button.addEventListener('click', () => {
      const visible = input.type === 'password';
      input.type = visible ? 'text' : 'password';
      button.textContent = visible ? 'Скрыть' : 'Показать';
      button.setAttribute('aria-pressed', String(visible));
    });
  }
  const login = document.querySelector('#login-form');
  if (login) {
    const button = login.querySelector('[type="submit"]'), error = document.querySelector('#login-error');
    button.disabled = true;
    try { await loadSession(); button.disabled = false; if (state.authenticated) location.replace('/'); }
    catch (failure) { showError(error, failure); }
    bindPasswordToggle(document.querySelector('#toggle-password'));
    login.addEventListener('submit', async (event) => {
      event.preventDefault(); button.disabled = true; error.textContent = '';
      try {
        await post('login', Object.fromEntries(new FormData(login)));
        login.reset(); location.replace('/');
      } catch (failure) { showError(error, failure); }
      finally { button.disabled = false; }
    });
    return;
  }
  const accountButton = document.querySelector('#account-button');
  if (!accountButton) return;
  try { await loadSession(); if (!state.authenticated) { location.replace('/login'); return; } }
  catch { return; }
  // Only trusted input renews a session. Focus/visibility changes and polling do not.
  // A trailing report preserves the last action without a request per mouse move.
  const ACTIVITY_INTERVAL = 15000;
  let lastAction = null, lastReport = -Infinity, activityTimer = null, reporting = false;
  async function reportActivity() {
    activityTimer = null;
    if (reporting || lastAction === null) return;
    const action = lastAction;
    const age = Math.max(0, (Date.now() - action) / 1000);
    if (age >= 1800) { lastAction = null; return; }
    reporting = true; lastReport = Date.now();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 6000);
    try {
      await post('activity', {age_seconds: age}, controller.signal);
      if (lastAction === action) lastAction = null;
    } catch (failure) {
      if (failure.status === 401 || failure.status === 403) location.replace('/login');
    } finally {
      clearTimeout(timeout); reporting = false;
      if (lastAction !== null) scheduleActivity();
    }
  }
  function scheduleActivity() {
    if (reporting || activityTimer !== null) return;
    const delay = Math.max(0, ACTIVITY_INTERVAL - (Date.now() - lastReport));
    if (delay === 0) reportActivity();
    else activityTimer = setTimeout(reportActivity, delay);
  }
  function userActivity(event) {
    if (!event.isTrusted || document.visibilityState !== 'visible') return;
    lastAction = Date.now(); scheduleActivity();
  }
  for (const name of ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart']) {
    document.addEventListener(name, userActivity, {passive: true, capture: true});
  }
  const notice = document.createElement('dialog');
  notice.className = 'auth-dialog'; notice.setAttribute('aria-labelledby', 'notice-title');
  notice.innerHTML = `<button type="button" class="auth-close" aria-label="Закрыть">×</button>
    <h2 id="notice-title">Смените стандартные данные</h2>
    <p>Логин и пароль admin / admin известны всем. Настоятельно рекомендуем заменить их. Можно сделать это позже в разделе «Учётная запись».</p>
    <div class="auth-controls"><button type="button" class="notice-cancel">Отмена</button><button type="button" class="auth-primary notice-change">Изменить</button></div>`;
  const dialog = document.createElement('dialog');
  dialog.className = 'auth-dialog settings-dialog'; dialog.setAttribute('aria-labelledby', 'account-title');
  dialog.innerHTML = `<button type="button" class="auth-close" aria-label="Закрыть">×</button>
    <h2 id="account-title">Настройки</h2><section class="settings-section account-section"><h3>Учётная запись</h3><form class="auth-form account-form">
    <label>Текущий логин<input name="current_username" readonly autocomplete="off"></label>
    <label>Текущий пароль<span class="password-field"><input id="account-current-password" aria-label="Текущий пароль" name="current_password" type="password" autocomplete="current-password" required maxlength="256"><button type="button" aria-controls="account-current-password" aria-pressed="false">Показать</button></span></label>
    <label>Новый логин<input name="username" autocomplete="username" required maxlength="64"></label>
    <label>Новый пароль<span class="password-field"><input id="account-new-password" aria-label="Новый пароль" name="password" type="password" autocomplete="new-password" required minlength="8" maxlength="256"><button type="button" aria-controls="account-new-password" aria-pressed="false">Показать</button></span></label>
    <label>Повтор нового пароля<span class="password-field"><input id="account-repeat-password" aria-label="Повтор нового пароля" name="password_confirm" type="password" autocomplete="new-password" required minlength="8" maxlength="256"><button type="button" aria-controls="account-repeat-password" aria-pressed="false">Показать</button></span></label>
    <p class="auth-hint">Пароль: от 8 до 256 символов. Другие активные сессии будут завершены.</p>
    <p class="auth-error" role="alert"></p><div class="auth-controls"><button type="button" class="account-cancel">Отмена</button><button type="submit" class="auth-primary">Сохранить</button></div></form></section>`;
  document.body.append(notice, dialog);
  const settingsForm = document.createElement('form');
  settingsForm.className = 'auth-form server-settings';
  settingsForm.innerHTML = '<h3>Название сервера</h3><label>Название сервера<input name="server_name" autocomplete="off" placeholder="До 24 символов" aria-describedby="server-name-status"></label><p id="server-name-status" class="auth-error name-status" role="status"></p><div class="auth-controls"><button type="button" data-later>Позже</button><button type="submit" class="auth-primary">Сохранить</button></div>';
  dialog.insertBefore(settingsForm, dialog.querySelector('.account-section'));
  const themeSection = document.createElement('section');
  themeSection.className = 'settings-section theme-setting';
  themeSection.innerHTML = '<h3 id="theme-title">Тема оформления</h3><div class="theme-options" role="group" aria-labelledby="theme-title"><button type="button" data-theme="light" aria-pressed="false">Светлая</button><button type="button" data-theme="dark" aria-pressed="false">Тёмная</button></div>';
  function syncTheme() { themeSection.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.theme === document.documentElement.dataset.theme))); }
  themeSection.querySelectorAll('button').forEach(b => b.onclick = () => { window.crdnApplyTheme(b.dataset.theme); syncTheme(); });
  dialog.insertBefore(themeSection, settingsForm); syncTheme();
  settingsForm.querySelector('[data-later]').onclick = () => dialog.close();
  const nameInput = settingsForm.elements.server_name;
  const nameStatus = settingsForm.querySelector('.name-status');
  function nameError(message) { nameStatus.dataset.state = 'error'; nameStatus.textContent = message; }
  function updateNameStatus(prefix = '') {
    const remaining = 24 - [...nameInput.value].length;
    if (remaining < 0) { nameError('Превышено на ' + (-remaining) + ' символов. Максимум 24.'); return; }
    nameStatus.dataset.state = 'ready';
    nameStatus.textContent = prefix + 'Осталось ' + remaining + ' символов';
  }
  nameInput.addEventListener('input', () => updateNameStatus());
  settingsForm.onsubmit = async event => {
    event.preventDefault();
    const name = settingsForm.elements.server_name.value;
    if ([...name].length > 24) { nameError('Не более 24 символов, включая пробелы.'); return; }
    const button = settingsForm.querySelector('[type="submit"]'); button.disabled = true;
    try {
      await loadSession();
      const response = await fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf_token}, body: JSON.stringify({server_name:name})});
      const value = await response.json();
      if (!response.ok) throw Error(value.error || 'Не удалось сохранить название.');
      window.crdnSetServerName(value.server_name); nameInput.value = value.server_name; settingsForm.querySelector('[data-later]').hidden = Boolean(value.server_name); updateNameStatus('Название сохранено. ');
    } catch (failure) { nameError(failure.message || 'Не удалось сохранить название. Повторите попытку.'); }
    finally { button.disabled = false; }
  };
  const form = dialog.querySelector('.account-form'), error = form.querySelector('.auth-error');
  const passwordToggles = [...dialog.querySelectorAll('[aria-controls]')];
  passwordToggles.forEach(bindPasswordToggle);
  async function dismiss() {
    notice.close();
    try { await post('dismiss'); state.remind_default = false; } catch { /* A later login will remind again. */ }
  }
  async function openAccount() {
    try { await loadSession(); } catch { return; }
    if (!state.authenticated) { location.replace('/login'); return; }
    form.reset(); form.elements.current_username.value = state.username;
    form.elements.current_password.value = state.default_credentials ? 'admin' : '';
    for (const button of passwordToggles) {
      document.getElementById(button.getAttribute('aria-controls')).type = 'password';
      button.textContent = 'Показать'; button.setAttribute('aria-pressed', 'false');
    }
    error.textContent = '';
    updateNameStatus();
    try { const r = await fetch('/api/settings', {cache:'no-store'}); if (r.ok) { const name = (await r.json()).server_name; settingsForm.elements.server_name.value = name; settingsForm.querySelector('[data-later]').hidden = Boolean(name); } } catch {}
    updateNameStatus(); syncTheme(); dialog.showModal();
  }
  function closeAccount() { dialog.close(); form.reset(); error.textContent = ''; }
  notice.querySelector('.auth-close').onclick = dismiss;
  notice.querySelector('.notice-cancel').onclick = dismiss;
  notice.addEventListener('cancel', event => { event.preventDefault(); dismiss(); });
  notice.querySelector('.notice-change').onclick = async () => { await dismiss(); openAccount(); };
  accountButton.onclick = openAccount;
  dialog.querySelector('.auth-close').onclick = closeAccount;
  dialog.querySelector('.account-cancel').onclick = closeAccount;
  dialog.addEventListener('cancel', event => { event.preventDefault(); closeAccount(); });
  form.addEventListener('submit', async (event) => {
    event.preventDefault(); const button = form.querySelector('[type="submit"]'); button.disabled = true; error.textContent = '';
    try {
      await post('change', Object.fromEntries(new FormData(form)));
      closeAccount();
    } catch (failure) { showError(error, failure); }
    finally { button.disabled = false; }
  });
  document.querySelector('#logout-button').onclick = async () => {
    try { await post('logout'); location.replace('/login'); }
    catch { alert('Выход не выполнен. Проверьте соединение и повторите.'); }
  };
  if (state.remind_default) { console.info('[CRDN auth]', 'default-credentials-notice'); notice.showModal(); }
})();
