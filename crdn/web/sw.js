'use strict';
// No fetch handler or Cache API: private pages, credentials and metrics stay network-only.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
