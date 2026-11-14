'use strict';
(() => {
  const version = document.querySelector('#current-version');
  if (!version) return;
  const offer = document.querySelector('#update-offer'), dialog = document.querySelector('#update-dialog');
  const title = document.querySelector('#update-title'), progress = document.querySelector('#update-progress');
  const confirm = document.querySelector('#update-confirm'), cancel = document.querySelector('#update-cancel');
  const manual = document.querySelector('#update-check'), note = document.querySelector('#update-check-note');
  const checkDialog = document.querySelector('#check-dialog'), checkOffer = document.querySelector('#check-offer');
  let manualRequested = false, manualError = false;
  const login = document.querySelector('#update-login');
  const phases = {queued: 'Обновление ожидает запуска…', download: 'Скачивание выпуска…', verify: 'Проверка файлов…',
    server: 'Проверка сервера и загрузка приложения…', probe: 'Пробный запуск с временными данными…',
    activate: 'Перезапуск панели и проверка результата…', health: 'Проверка установленной версии…',
    interrupted: 'Обновление прервано. Требуется проверка через SSH.',
    failed_rolled_back: 'Обновление не выполнено. Сохранена прежняя версия и данные входа.',
    failed: 'Обновление не подтверждено. Требуется проверка через SSH.'};
  const busy = new Set(['queued', 'download', 'verify', 'server', 'probe', 'activate', 'health']);
  let state, target, submitted = false, showingResult = false, disconnectedAt;
  const releaseVersion = value => typeof value === 'string' && /^v\d+\.\d+\.\d+(?:-rc\.[1-9]\d*)?$/.test(value) ? value : null;
  const currentLabel = value => releaseVersion(value.current) || 'не определена';
  function remembered(value) { try { if (value === undefined) return sessionStorage.getItem('crdn-update-target'); if (value) sessionStorage.setItem('crdn-update-target', value); else sessionStorage.removeItem('crdn-update-target'); } catch {} return null; }
  async function api(path, post = false) {
    const options = {cache: 'no-store', signal: AbortSignal.timeout(5000)};
    if (post) {
      const session = await fetch('/api/auth/session', options).then(r => r.json());
      options.method = 'POST'; options.headers = {'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf_token}; options.body = JSON.stringify(path === '/start' ? {version: target} : {});
    }
    const response = await fetch('/api/updates' + path, options);
    if (response.status === 401 || response.status === 403) {
      diagnostic('http-' + response.status);
      const error = Error('Сессия завершилась. Войдите снова, чтобы проверить результат.'); error.login = true; throw error;
    }
    const value = await response.json();
    if (!response.ok || value.error) throw Error(value.error || 'Не удалось выполнить запрос.');
    return value;
  }
  function renderNotes(value) {
    document.querySelector('#update-versions').textContent = 'Установлена: ' + currentLabel(value) + (releaseVersion(target) ? ' → Доступна: ' + target : '');
    const content = document.querySelector('#release-notes-content');
    content.replaceChildren();
    const notes = value.release_notes;
    if (!notes || notes.version !== target || value.available !== target || !['added','fixed'].every(k => Array.isArray(notes[k]) && notes[k].every(t => typeof t === 'string'))) {
      content.textContent = 'Список изменений временно недоступен. Можно подтвердить обновление выбранной версии.';
      return;
    }
    let remaining = 1200, found = false;
    for (const [key, label] of [['added','Добавлено'],['fixed','Исправлено']]) {
      const lines = notes[key].slice(0,12).filter(Boolean);
      if (!lines.length || !remaining) continue;
      found = true;
      const heading = document.createElement('h4'), list = document.createElement('ul');
      heading.textContent = label;
      for (const text of lines) {
        if (!remaining) break;
        const item = document.createElement('li'), clipped = [...text].slice(0,Math.min(240,remaining));
        item.textContent = clipped.join(''); remaining -= clipped.length; list.append(item);
      }
      content.append(heading,list);
    }
    if (!found) content.textContent = 'Список изменений временно недоступен. Можно подтвердить обновление выбранной версии.';
  }
  function render(value) {
    diagnostic(value.enabled === false ? 'controller-unavailable' : value.operation?.phase || (value.check_error ? 'source-unavailable' : 'idle'));
    state = value;
    if (dialog.open && !submitted && !showingResult) renderNotes(value);
    version.textContent = 'Версия: ' + currentLabel(value);
    document.querySelector('#about-version').textContent = version.textContent;
    checkOffer.hidden = !value.available;
    checkOffer.textContent = value.available ? 'Обновить до ' + value.available : 'Обновить';
    document.querySelector('#update-channel').textContent = value.channel === 'rc' ? 'Включён тестовый канал RC.' : 'Канал стабильных выпусков.';
    offer.hidden = !value.available;
    if (value.available) offer.textContent = 'Доступна ' + value.available + ' · Обновить';
    note.textContent = manualError ? 'Проверка не выполнена. Проверьте соединение и повторите.' : value.checking ? 'Проверка выпусков…' : value.check_error ? 'Источники временно недоступны. Повторите проверку позже.' : value.available ? 'Доступен выпуск ' + value.available : manualRequested && value.checked_at ? 'Установлена последняя доступная версия' : 'Проверка наличия обновлений';
    if (value.enabled === false) {
      note.textContent = value.message || 'Системный механизм обновления временно недоступен.'; manual.disabled = true;
      if (submitted) progress.textContent = 'Системный механизм перезапускается. Ожидание статуса обновления…';
      return;
    }
    manual.disabled = value.checking;
    const operation = value.operation;
    if (!operation || !releaseVersion(operation.target)) return;
    if (busy.has(operation.phase)) {
      document.querySelector('#release-notes').hidden = true;
      target = operation.target; remembered(target); submitted = true; window.crdnUpdateActive = true;
      title.textContent = 'Обновление CRDN до ' + target;
      progress.textContent = phases[operation.phase]; confirm.disabled = true; cancel.disabled = true;
      if (!dialog.open) dialog.showModal();
    } else if ((submitted && operation.target === target) || remembered() === operation.target || (showingResult && dialog.open && operation.target === target)) {
      target = operation.target; showingResult = true;
      document.querySelector('#update-versions').textContent = 'Установлена: ' + currentLabel(value);
      document.querySelector('#release-notes').hidden = true;
      if (remembered() === operation.target) { title.textContent = 'Результат обновления CRDN'; if (!dialog.open) dialog.showModal(); remembered(null); }
      window.crdnUpdateActive = false; submitted = false; cancel.disabled = false;
      confirm.hidden = true; cancel.textContent = 'Закрыть';
      progress.textContent = operation.phase === 'success' && value.current === operation.target ? 'CRDN обновлён до ' + value.current + '. Данные входа и настройки сохранены.' : (phases[operation.phase] || 'Результат обновления не подтверждён.');
      if (operation.reason === 'integrity') progress.textContent += ' Ошибка проверки целостности файлов.';
    }
  }
  offer.addEventListener('click', () => {
    if (submitted || !state?.available) return;
    checkDialog.close();
    target = state.available; showingResult = false; title.textContent = 'Обновить CRDN до ' + target + '?';
    progress.textContent = ''; confirm.hidden = false; confirm.disabled = false; cancel.disabled = false; cancel.textContent = 'Отмена'; login.hidden = true;
    document.querySelector('#release-notes').hidden = false;
    renderNotes(state);
    dialog.showModal();
  });
  cancel.addEventListener('click', () => { if (!submitted) dialog.close(); });
  dialog.addEventListener('cancel', event => { if (submitted) event.preventDefault(); });
  confirm.addEventListener('click', async () => {
    if (submitted) return;
    submitted = true; remembered(target); window.crdnUpdateActive = true; confirm.disabled = true; cancel.disabled = true;
    progress.textContent = 'Отправка запроса…';
    try { render(await api('/start', true)); }
    catch (error) {
      // A lost response can follow an accepted request; ask persisted status before retrying.
      progress.textContent = error.message + ' Проверяем состояние операции…';
      if (error.login) login.hidden = false;
    }
    poll();
  });
  manual.addEventListener('click', async () => {
    manualRequested = true; manualError = false; manual.disabled = true; note.textContent = 'Проверка выпусков…';
    try { render(await api('/check', true)); }
    catch (error) { manualError = true; note.textContent = error.login ? error.message : 'Проверка не выполнена. Проверьте соединение и повторите.'; }
    finally { manual.disabled = state?.enabled === false || Boolean(state?.checking); }
  });
  checkOffer.onclick = () => offer.click();
  document.querySelector('#open-update-check').onclick = () => { checkDialog.showModal(); manual.click(); };
  // Safe browser-only diagnostics: event codes, phases and HTTP status, no bodies or tokens.
  let lastDiagnostic;
  function diagnostic(code) { if (lastDiagnostic !== code) { console.info('[CRDN updates]', code); lastDiagnostic = code; } }
  async function poll() {
    try {
      const value = await api(''); disconnectedAt = null;
      // The root worker's success must also match the running HTTP release.
      render(value);
      if (value.enabled === false) return;
      if (submitted && (!value.operation || (!busy.has(value.operation.phase) && value.operation.target !== target))) {
        submitted = false; remembered(null); window.crdnUpdateActive = false; cancel.disabled = false; confirm.disabled = false;
        progress.textContent = 'Запуск обновления не подтверждён. Проверьте наличие выпуска и повторите запрос.';
      }
    } catch (error) {
      diagnostic(error.login ? 'reauth-required' : 'status-connection-lost');
      if (submitted) {
        disconnectedAt ||= Date.now();
        progress.textContent = error.login ? error.message : 'Связь с панелью временно потеряна. Ожидание перезапуска…';
        if (error.login || Date.now() - disconnectedAt > 120000) login.hidden = false;
      }
    }
  }
  poll();
  setInterval(poll, 5000);
})();
