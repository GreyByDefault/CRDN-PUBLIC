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
  const login = document.querySelector('#login-form');
  if (login) {
    const button = login.querySelector('[type="submit"]'), error = document.querySelector('#login-error');
    button.disabled = true;
    try { await loadSession(); button.disabled = false; if (state.authenticated) location.replace('/'); }
    catch (failure) { showError(error, failure); }
    document.querySelector('#toggle-password').addEventListener('click', (event) => {
      const input = document.querySelector('#login-password');
      const visible = input.type === 'password'; input.type = visible ? 'text' : 'password';
      event.currentTarget.textContent = visible ? 'Скрыть' : 'Показать';
      event.currentTarget.setAttribute('aria-pressed', String(visible));
    });
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
  dialog.className = 'auth-dialog'; dialog.setAttribute('aria-labelledby', 'account-title');
  dialog.innerHTML = `<button type="button" class="auth-close" aria-label="Закрыть">×</button>
    <h2 id="account-title">Учётная запись</h2><form class="auth-form">
    <label>Текущий пароль<input name="current_password" type="password" autocomplete="current-password" required maxlength="256"></label>
    <label>Новый логин<input name="username" autocomplete="username" required maxlength="64"></label>
    <label>Новый пароль<input name="password" type="password" autocomplete="new-password" required minlength="8" maxlength="256"></label>
    <label>Подтверждение пароля<input name="password_confirm" type="password" autocomplete="new-password" required minlength="8" maxlength="256"></label>
    <p class="auth-hint">Пароль: от 8 до 256 символов. Другие активные сессии будут завершены.</p>
    <p class="auth-error" role="alert"></p><div class="auth-controls"><button type="button" class="account-cancel">Отмена</button><button type="submit" class="auth-primary">Сохранить</button></div></form>`;
  document.body.append(notice, dialog);
  const form = dialog.querySelector('form'), error = form.querySelector('.auth-error');
  async function dismiss() {
    notice.close();
    try { await post('dismiss'); state.remind_default = false; } catch { /* A later login will remind again. */ }
  }
  function openAccount() {
    form.reset(); form.elements.username.value = state.username; error.textContent = '';
    dialog.showModal();
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
  if (state.remind_default) notice.showModal();
})();
