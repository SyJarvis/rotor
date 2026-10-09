import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../src/rotor/frontend/pages/chat.js', import.meta.url), 'utf8');
const translations = await readFile(new URL('../src/rotor/frontend/config.js', import.meta.url), 'utf8');
const appSource = await readFile(new URL('../src/rotor/frontend/app.js', import.meta.url), 'utf8');
const tick = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

async function environment({ availableModels = ['model-a', 'model-b'], language = 'zh-CN', viewportWidth = 1200,
  storage = new Map(), attachments = new Map(), storageFailure = false, storageReadFailure = false, attachmentFailure = false,
  streams = [], modelResponses = [], probes = [], tokenAvailable = true, reducedMotion = false, attachmentReads = null } = {}) {
  const nodes = new Map(), actions = new Map(), listeners = {}, requests = [], downloads = [], notices = [];
  let nextId = 0, focused, unauthorized = 0;
  function parse(html, ancestors = '') {
    for (const tag of html.matchAll(/<[^>]+>/g)) {
      const id = tag[0].match(/\bid="([^"]+)"/)?.[1];
      const action = tag[0].match(/\bdata-agent-action="([^"]+)"/)?.[1];
      let parent = ancestors;
      if (['attachment-menu', 'add-image', 'add-file'].includes(action)) parent = '.agent-add-wrap';
      if (['history', 'new'].includes(action)) parent = '.agent-left-controls';
      if (action === 'details') parent = '.agent-details-trigger';
      if (id === 'chatHistorySearch') parent = '.agent-history-drawer';
      const node = element(id, action, parent);
      (tag[0].match(/\bclass="([^"]+)"/)?.[1] || '').split(' ').filter(Boolean).forEach((name) => node.classList.add(name));
      node.value = tag[0].match(/\bvalue="([^"]*)"/)?.[1] || '';
      if (id) nodes.set(id, node);
      if (action && !actions.has(action)) actions.set(action, node);
    }
  }
  function element(id = '', action = '', ancestors = '') {
    const classes = new Set(), attrs = {};
    let html = '';
    return {
      id, dataset: { agentAction: action }, value: '', style: {}, listeners: {}, disabled: false, textContent: '', scrollHeight: 80, clientHeight: 80, scrollTop: 0,
      scrollTo(options) { this.lastScroll = options; this.scrollTop = options.top; },
      get innerHTML() { return html; }, set innerHTML(value) { html = value; parse(value, id === 'agentDetailsContent' ? '.agent-drawer' : ancestors); },
      classList: { add: (n) => classes.add(n), remove: (n) => classes.delete(n), contains: (n) => classes.has(n),
        toggle(n, force) { const yes = force ?? !classes.has(n); yes ? classes.add(n) : classes.delete(n); return yes; } },
      setAttribute: (name, value) => { attrs[name] = value; }, getAttribute: (name) => attrs[name],
      addEventListener(name, callback) { this.listeners[name] = callback; },
      querySelector: (selector) => query(selector),
      querySelectorAll: (selector) => selector === '[data-agent-action="close-drawer"]'
        ? [nodes.get("agentDrawerBackdrop"), nodes.get("agentHistoryDrawer-close"), nodes.get("agentDetailsDrawer-close")].filter(Boolean)
        : [query(selector)].filter(Boolean),
      closest(selector) { if (selector === '[data-agent-action]') return action ? this : null; return selector.split(',').some((part) => ancestors.includes(part.trim())) ? this : null; },
      focus() { focused = this; }, click() { downloads.push(this); }, remove() {}, dispatchEvent(event) { this.listeners[event.type]?.({ currentTarget: this }); },
    };
  }
  function query(selector) {
    if (selector === '#chat .agent-chat-shell') return nodes.get('chat')?.innerHTML ? nodes.get('chat') : null;
    const close = selector.match(/^#(agentHistoryDrawer|agentDetailsDrawer) \[data-agent-action="close-drawer"\]$/);
    if (close) return nodes.get(`${close[1]}-close`);
    const action = selector.match(/^\[data-agent-action="([^"]+)"\]$/);
    if (action) return actions.get(action[1]);
    if (selector.startsWith('#')) return nodes.get(selector.slice(1));
    return null;
  }
  nodes.set('chat', element('chat'));
  for (const id of ['chatDeleteModal', 'chatDeleteConversationName']) nodes.set(id, element(id));
  nodes.get('chatDeleteModal').classList.add('hidden');
  for (const id of ['agentHistoryDrawer', 'agentDetailsDrawer']) nodes.set(`${id}-close`, element('', 'close-drawer', `.agent-drawer #${id} ${id === 'agentHistoryDrawer' ? '.agent-history-drawer' : '.agent-details-drawer'}`));
  const translationWindow = {};
  vm.runInNewContext(translations, { window: translationWindow });
  const t = (key) => translationWindow.ROTOR_UI_CONFIG.locales[language][key] ?? key;
  const document = {
    documentElement: { lang: language }, getElementById: (id) => nodes.get(id), querySelector: query,
    addEventListener(name, callback) { listeners[name] = callback; }, dispatchEvent() {},
    createElement: () => element(), body: { appendChild() {} },
  };
  const context = vm.createContext({
    document, window: { innerHeight: 800, innerWidth: viewportWidth, matchMedia: () => ({ matches: reducedMotion }) },
    localStorage: { getItem: (key) => { if (storageReadFailure) throw new Error('storage unavailable'); return storage.get(key); }, setItem: (key, value) => { if (storageFailure) throw new Error('quota'); storage.set(key, value); } },
    crypto: { randomUUID: () => `new-${++nextId}` },
    CustomEvent: class { constructor(name) { this.type = name; } }, Event, Date, Blob, TextDecoder, TextEncoder, Uint8Array, AbortController,
    FileReader: class { readAsDataURL(file) { this.result = file.dataUrl || `data:${file.type};base64,QUJD`; this.onload(); } },
    URL: { createObjectURL: (blob) => { downloads.push({ blob }); return 'blob:export'; }, revokeObjectURL() {} }, setTimeout: (callback) => callback(), clearTimeout() {},
    fetch: async (url, options) => {
      requests.push({ url, options, payload: JSON.parse(options.body) });
      const stream = streams.shift() || {};
      if (stream.response) return stream.response;
      let index = 0;
      const chunks = stream.chunks || ['data: {"type":"delta","content":"draft"}\n\ndata: {"type":"done","answer":"final answer"}\n\n'];
      return { ok: true, body: { getReader: () => ({ read: async () => {
        const gate = stream.gates?.[index] || (index === 0 ? stream.gate : null);
        if (gate) {
          await Promise.race([gate.promise, new Promise((_, reject) => options.signal.addEventListener('abort', () => { if (!stream.ignoreAbort) reject(Object.assign(new Error('stopped'), { name: 'AbortError' })); }))]);
        }
        if (index >= chunks.length) return { done: true };
        stream.onChunk?.(index);
        return { value: new TextEncoder().encode(chunks[index++]), done: false };
      }, releaseLock() {}, cancel() {} }) } };
    },
  });
  const bindings = {
    t, api: async (url, options) => {
      if (url.endsWith('/models')) return modelResponses.length ? await modelResponses.shift() : { models: availableModels, token_available: tokenAvailable };
      requests.push({ url, options, payload: JSON.parse(options.body) });
      return probes.length ? await probes.shift() : { success: true, latency_ms: 12.8 };
    },
    adminCsrfHeaders: () => ({ 'X-Test-Csrf': 'present' }), handleAdminUnauthorized(response) { if (response.status === 401) unauthorized++; }, copyText: async () => true,
    escapeHtml: (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
    refreshIcons() {}, toast(message, severity) { notices.push({ message, severity }); },
    saveAttachment: async (attachment) => { if (attachmentFailure) throw new Error('quota'); attachments.set(attachment.id, structuredClone(attachment)); },
    getAttachment: async (id) => attachmentReads ? await attachmentReads(id) : attachments.get(id), deleteAttachments: async (ids) => { ids.forEach((id) => attachments.delete(id)); },
  };
  const module = new vm.SourceTextModule(source, { context });
  await module.link((specifier) => {
    const names = specifier.includes('i18n') ? ['t'] : specifier.includes('api.js') ? ['adminCsrfHeaders', 'api', 'copyText', 'handleAdminUnauthorized']
      : specifier.includes('chat-attachments') ? ['saveAttachment', 'getAttachment', 'deleteAttachments'] : ['escapeHtml', 'refreshIcons', 'toast'];
    return new vm.SyntheticModule(names, function () { names.forEach((name) => this.setExport(name, bindings[name])); }, { context });
  });
  await module.evaluate();
  await module.namespace.load();
  return {
    page: module.namespace, nodes, actions, listeners, storage, requests, downloads, notices, attachments,
    setLocale(value) { language = value; document.documentElement.lang = value; },
    current: () => JSON.parse(storage.get('rotor.chat.conversations.v1')).find((item) => item.id === storage.get('rotor.chat.active.v1')),
    target(action, dataset = {}, ancestors = '.agent-drawer') { const node = element('', action, ancestors); Object.assign(node.dataset, dataset); return node; },
    outside: () => element(), focused: () => focused, unauthorized: () => unauthorized,
    change(id, value) { const node = nodes.get(id); node.value = value; (node.listeners.input || node.listeners.change)({ target: node, currentTarget: node }); },
    addFiles(files, images = false) { return nodes.get(images ? 'agentImageInput' : 'agentFileInput').listeners.change({ currentTarget: { files, value: '' } }); },
    send(text = 'hello') { nodes.get('agentPrompt').value = text; return nodes.get('agentSend').listeners.click(); },
    testConnection() { return nodes.get('chatTestConnection').listeners.click(); },
  };
}

const file = (name = 'note.txt', content = 'notes') => ({ name, type: 'text/plain', size: content.length, text: async () => content });
const image = () => ({ name: 'photo.png', type: 'image/png', size: 3, dataUrl: 'data:image/png;base64,QUJD' });

test('settings initially open, retain Chat defaults, and stay closed after a manual dismissal', async () => {
  const env = await environment();
  assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
  assert.equal(await env.page.onClick(env.target('', {}, '.nav-item[data-tab]')), false);
  assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
  assert.equal(env.current().settings.api_format, 'chat');
  assert.equal(env.current().settings.temperature, null);
  assert.equal(env.current().settings.max_tokens, null);
  assert.equal(env.nodes.get('agentModel').value, 'model-a');
  assert.doesNotMatch(env.nodes.get('chat').innerHTML, /web-search|agent-model-picker|MindAgent/);
  assert.match(env.nodes.get('agentDetailsContent').innerHTML, /System Prompt|Temperature|Max tokens/);
  assert.doesNotMatch(env.nodes.get('chat').innerHTML, /chatNewLabel/);
  await env.page.onClick(env.actions.get('details'));
  assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), true);
  await env.page.onClick(env.actions.get('details'));
  assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
  assert.equal(env.nodes.get('agentDrawerBackdrop').classList.contains('hidden'), true);
  await env.page.onClick(env.outside());
  assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
  await env.page.onClick(env.nodes.get('agentDetailsDrawer-close'));
  assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), true);
  await env.page.load();
  env.setLocale('en'); await env.page.load(); env.page.renderHistory();
  assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), true);
  await env.send('remain closed');
  assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), true);
});

test('settings persist by conversation and streaming requests carry a validated snapshot', async () => {
  const gate = deferred();
  const env = await environment({ streams: [{ gate }] });
  env.change('agentModel', 'model-b'); env.change('chatApiFormat', 'responses'); env.change('chatSystemPrompt', 'Be concise');
  env.change('chatTemperature', '0.4'); env.change('chatMaxTokens', '256');
  const sending = env.send('question'); await tick();
  const request = env.requests[0];
  assert.equal(request.url, '/api/admin/chat/completions');
  assert.equal(request.options.credentials, 'same-origin');
  assert.equal(request.options.headers['X-Test-Csrf'], 'present');
  assert.equal(request.payload.model, 'model-b'); assert.equal(request.payload.api_format, 'responses');
  assert.equal(request.payload.system_prompt, 'Be concise'); assert.equal(request.payload.temperature, 0.4); assert.equal(request.payload.max_tokens, 256);
  env.change('chatTemperature', '0.9');
  assert.equal(request.payload.temperature, 0.4);
  gate.resolve(); await sending;
  const old = env.current().id;
  await env.page.onClick(env.actions.get('new'));
  assert.equal(env.current().settings.api_format, 'responses');
  assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
  env.change('chatSystemPrompt', 'New conversation settings');
  await env.page.onClick(env.target('open', { conversationId: old }));
  assert.equal(env.current().settings.temperature, '0.9');
  assert.equal(env.current().settings.system_prompt, 'Be concise');
});

for (const [id, values] of [['chatTemperature', ['-1', '2.1', 'NaN']], ['chatMaxTokens', ['0', '-4', '1.5', '1000001', '1e20']]]) {
  test(`${id} rejects invalid values before either request`, async () => {
    const env = await environment();
    for (const value of values) {
      env.change(id, value); await env.send(); await env.testConnection();
      assert.equal(env.requests.length, 0);
      assert.equal(env.nodes.get('chatSettingsError').classList.contains('hidden'), false);
    }
    env.change(id, ''); await env.send();
    assert.equal(env.requests.length, 1);
  });
}

test('connection test only sends settings, reports latency and becomes stale on any change', async () => {
  const gate = deferred();
  const env = await environment({ probes: [{ success: true, latency_ms: 24.2 }, gate.promise] });
  await env.send('private history');
  await env.testConnection();
  const request = env.requests[1];
  assert.equal(request.url, '/api/admin/chat/test'); assert.equal(request.options.method, 'POST');
  assert.deepEqual(Object.keys(request.payload).sort(), ['api_format', 'max_tokens', 'model', 'system_prompt', 'temperature']);
  assert.match(env.nodes.get('chatProbeResult').textContent, /24 ms/);
  env.change('chatSystemPrompt', 'Changed');
  assert.equal(env.nodes.get('chatProbeResult').textContent, '');
  const probing = env.testConnection(); await tick();
  env.change('agentModel', 'model-b');
  assert.equal(env.requests[2].options.signal.aborted, true);
  gate.resolve({ success: true, latency_ms: 99 }); await probing;
  assert.equal(env.nodes.get('chatProbeResult').textContent, '');
});

test('images, text and binary documents retain payloads across turns and page reload', async () => {
  const env = await environment();
  await env.addFiles([image()], true);
  await env.addFiles([file(), { name: 'paper.pdf', type: '', size: 3, dataUrl: 'data:application/octet-stream;base64,QUJD' }, { name: 'paper.docx', type: '', size: 3, dataUrl: 'data:application/octet-stream;base64,QUJD' }]);
  assert.match(env.nodes.get('agentAttachmentStrip').innerHTML, /data:image\/png/);
  await env.send('review');
  const attachments = env.requests[0].payload.messages[0].attachments;
  assert.equal(attachments.length, 4); assert.equal(attachments[1].content, 'notes');
  assert.match(attachments[2].data_url, /^data:application\/pdf;/);
  assert.match(attachments[3].data_url, /^data:application\/vnd.openxmlformats-officedocument.wordprocessingml.document;/);
  const stored = env.storage.get('rotor.chat.conversations.v1');
  assert.doesNotMatch(stored, /base64|"content":"notes"/);
  const reload = await environment({ storage: env.storage, attachments: env.attachments });
  await reload.send('follow up');
  assert.deepEqual(reload.requests[0].payload.messages[0].attachments, attachments);
  assert.equal(reload.requests[0].payload.messages.at(-1).content, 'follow up');
});

test('old MindAgent conversations migrate without deleting old data and missing attachments are explicit', async () => {
  const old = [{ id: 'legacy', model: 'model-b', title: 'old', messages: [{ id: 'u', role: 'user', content: 'look', attachments: [{ kind: 'image', name: 'old.png', size: 4 }] }], updatedAt: '2025-01-01' }];
  const storage = new Map([['rotor.mindagent.conversations.v1', JSON.stringify(old)], ['rotor.mindagent.active.v1', 'legacy']]);
  const env = await environment({ storage });
  assert.equal(env.current().id, 'legacy'); assert.equal(env.current().settings.model, 'model-b');
  assert.equal(storage.get('rotor.mindagent.conversations.v1'), JSON.stringify(old));
  assert.match(env.nodes.get('agentMessages').innerHTML, /内容未保存/);
  await env.send('continue');
  assert.match(env.requests[0].payload.messages[0].content, /Attachment content unavailable.*old.png/);
  assert.deepEqual(env.requests[0].payload.messages[0].attachments, []);
  assert.ok(env.notices.some((notice) => /历史附件内容不可用/.test(notice.message)));
});

test('attachment storage failure does not enqueue an unsaved file', async () => {
  const env = await environment({ attachmentFailure: true });
  await env.addFiles([file()]);
  assert.equal(env.attachments.size, 0);
  await env.send(''); assert.equal(env.requests.length, 0);
  assert.ok(env.notices.some((notice) => /附件未添加/.test(notice.message)));
});

test('file read finishing after a new conversation cannot attach to the new chat', async () => {
  const read = deferred(); const env = await environment();
  const pending = env.addFiles([{ ...file(), text: () => read.promise }]);
  await tick(); await env.page.onClick(env.actions.get('new')); read.resolve('late'); await pending;
  assert.equal(env.attachments.size, 0); await env.send(''); assert.equal(env.requests.length, 0);
});

test('SSE handles fragmented CRLF events and final event without trailing delimiter', async () => {
  const env = await environment({ streams: [{ chunks: ['data: {"type":"delta","cont', 'ent":"片段"}\r', '\n\r\ndata: {"type":"done","answer":"完整回答","usage":{"total_tokens":9}}'] }] });
  await env.send();
  assert.equal(env.current().messages.at(-1).content, '完整回答');
  assert.equal(env.current().messages.at(-1).usage.total_tokens, 9);
});

test('incomplete streams and structured errors are visible and not sent back as assistant context', async () => {
  const env = await environment({ streams: [{ chunks: ['data: {"type":"delta","content":"partial"}\n\n'] }, { response: { ok: false, status: 401, text: async () => JSON.stringify({ detail: 'Expired session' }) } }] });
  await env.send(); assert.equal(env.current().messages.at(-1).status, 'error');
  assert.equal(env.current().messages.at(-1).content, 'partial'); assert.match(env.current().messages.at(-1).error, /提前中断/); assert.equal(env.requests[0].options.signal.aborted, true);
  await env.send('again'); assert.equal(env.unauthorized(), 1);
  assert.match(env.current().messages.at(-1).error, /Expired session/);
  assert.equal(env.requests[1].payload.messages.filter((message) => message.role === 'assistant').length, 0);
});

for (const action of ['new', 'delete', 'unload']) {
  test(`${action} aborts generation and late completions cannot overwrite another conversation`, async () => {
    const oldGate = deferred(), newGate = deferred();
    const env = await environment({ streams: [{ gate: oldGate, ignoreAbort: true }, { gate: newGate }] });
    const oldId = env.current().id;
    const first = env.send('old'); await tick();
    if (action === 'delete') {
      await env.page.onClick(env.target('delete', { conversationId: oldId })); await env.page.onClick(env.target('confirm-delete'));
    } else if (action === 'unload') { env.page.unload(); await env.page.load(); await env.page.onClick(env.actions.get('new')); }
    else await env.page.onClick(env.actions.get('new'));
    assert.equal(env.requests[0].options.signal.aborted, true);
    const second = env.send('new'); await tick();
    oldGate.resolve(); await first;
    assert.equal(env.nodes.get('agentStop').classList.contains('hidden'), false);
    assert.equal(env.current().messages[0].content, 'new');
    env.nodes.get('agentStop').listeners.click(); await second;
    assert.equal(env.requests[1].options.signal.aborted, true);
    assert.equal(env.current().messages.at(-1).status, 'stopped');
  });
}

test('stale model loads and connection tests do not redraw after unload', async () => {
  const slow = deferred();
  const env = await environment({ modelResponses: [{ models: ['model-a'], token_available: true }, slow.promise, { models: ['model-b'], token_available: true }] });
  const first = env.page.load();
  await env.page.load();
  slow.resolve({ models: ['old-model'], token_available: true }); await first;
  assert.match(env.nodes.get('agentModel').innerHTML, /model-b/); assert.doesNotMatch(env.nodes.get('agentModel').innerHTML, /old-model/);
});

for (const viewportWidth of [320, 700, 1024]) {
  test(`narrow history keeps outside and Escape controls without hiding settings at ${viewportWidth}px`, async () => {
    const env = await environment({ viewportWidth });
    assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
    await env.page.onClick(env.actions.get('details'));
    assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), true);
    await env.page.onClick(env.actions.get('details'));
    assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
    await env.page.onClick(env.outside()); assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
    await env.page.onClick(env.actions.get('history'));
    env.listeners.keydown({ key: 'Escape' }); assert.equal(env.nodes.get('agentHistoryDrawer').classList.contains('hidden'), true);
    assert.equal(env.focused(), env.actions.get('history'));
    assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
  });
}

test('export includes attachment bodies and deleting another chat keeps active generation and draft attachments', async () => {
  const gate = deferred(); const env = await environment({ streams: [{}, { gate }] });
  await env.addFiles([file()]); await env.send();
  const oldId = env.current().id;
  await env.page.onClick(env.target('export', { conversationId: oldId }));
  const exported = JSON.parse(await env.downloads[0].blob.text());
  assert.equal(exported.conversation.messages[0].attachments[0].content, 'notes');
  assert.match(env.downloads[1].download, /^rotor-conversation/);
  await env.page.onClick(env.actions.get('new'));
  const sending = env.send(); await tick(); await env.addFiles([file('next.txt')]);
  await env.page.onClick(env.target('delete', { conversationId: oldId })); await env.page.onClick(env.target('confirm-delete'));
  assert.equal(env.requests[1].options.signal.aborted, false);
  assert.match(env.nodes.get('agentAttachmentStrip').innerHTML, /next.txt/);
  assert.equal(env.attachments.size, 1); gate.resolve(); await sending;
});

for (const language of ['zh-CN', 'en']) {
  test(`settings labels and descriptions are translated (${language})`, async () => {
    const env = await environment({ language });
    assert.match(env.nodes.get('agentDetailsContent').innerHTML, language === 'en' ? /Test connection/ : /测试连接/);
    assert.doesNotMatch(env.nodes.get('agentDetailsContent').innerHTML, />chat\w+</);
  });
}

test('no-model, missing token and full localStorage failures are handled without throwing', async () => {
  const empty = await environment({ availableModels: [] }); await empty.send(); assert.equal(empty.requests.length, 0);
  const noToken = await environment({ tokenAvailable: false }); await noToken.send(); assert.equal(noToken.requests.length, 0);
  const quota = await environment({ storageFailure: true }); await quota.send();
  assert.equal(quota.requests.length, 1); assert.ok(quota.notices.some((notice) => /无法保存对话/.test(notice.message)));
});

test('all navigation routes use Chat and settings has no page navigation handler', async () => {
  const pages = ['overview', 'channels', 'tokens', 'usage', 'logs', 'monitoring', 'chat'];
  const loads = [], toggles = new Map();
  const node = (id, dataset = {}) => ({ id, dataset, listeners: {}, textContent: '', classList: { toggle(name, value) { toggles.set(`${id}:${name}`, value); } }, addEventListener(name, callback) { this.listeners[name] = callback; } });
  const navigation = pages.map((page) => node(page, { tab: page })), settings = node('openSettings'), panels = pages.map((page) => node(page));
  const other = new Map(['content', 'pageTitle', '.main', '.app-shell'].map((id) => [id, node(id)]));
  const context = vm.createContext({ PAGES: Object.fromEntries(pages.map((name) => [name, { load: () => loads.push(name), unload() {} }])), TITLES: Object.fromEntries(pages.map((name) => [name, () => name])), current: 'overview', closeSidebar() {}, refreshActive: async () => context.PAGES[context.current].load(), document: { getElementById: (id) => other.get(id), querySelector: (selector) => other.get(selector), querySelectorAll: (selector) => selector === '.nav-item[data-tab]' ? navigation : selector === '.panel' ? panels : selector === '.nav-item' ? [...navigation, settings] : [] } });
  vm.runInContext(appSource.slice(appSource.indexOf('function switchTab('), appSource.indexOf('document.addEventListener("chatconversationopen"')), context);
  for (const nav of navigation.slice(1)) await nav.listeners.click();
  assert.equal(loads.join(','), pages.slice(1).join(',')); assert.equal(toggles.get('.app-shell:chat-app'), true); assert.equal(settings.listeners.click, undefined);
});

test('saved missing models remain selected and are never silently replaced', async () => {
  const storage = new Map([['rotor.chat.conversations.v1', JSON.stringify([{ id: 'saved', model: 'removed-model', messages: [], settings: { model: 'removed-model', api_format: 'responses' } }])], ['rotor.chat.active.v1', 'saved']]);
  const env = await environment({ storage });
  assert.equal(env.nodes.get('agentModel').value, 'removed-model');
  assert.match(env.nodes.get('agentModel').innerHTML, /removed-model.*不可用/);
  await env.send(); assert.equal(env.requests.length, 0);
  env.change('agentModel', 'model-a'); await env.send(); assert.equal(env.requests[0].payload.model, 'model-a');
});

test('an old probe completion after unload cannot replace a later probe result', async () => {
  const old = deferred(), fresh = deferred();
  const env = await environment({ probes: [old.promise, fresh.promise] });
  const first = env.testConnection(); await tick(); env.page.unload(); await env.page.load();
  const second = env.testConnection(); await tick();
  old.resolve({ success: true, latency_ms: 111 }); await first;
  assert.equal(env.nodes.get('chatTestConnection').disabled, true);
  fresh.resolve({ success: true, latency_ms: 22 }); await second;
  assert.match(env.nodes.get('chatProbeResult').textContent, /22 ms/);
  assert.doesNotMatch(env.nodes.get('chatProbeResult').textContent, /111/);
});

test('text payload reports actual UTF-8 bytes and the six-attachment limit is enforced', async () => {
  const env = await environment();
  await env.addFiles([{ name: 'bom.txt', type: 'text/plain', size: 6, text: async () => '汉' }, ...Array.from({ length: 6 }, (_, index) => file(`${index}.txt`))]);
  await env.send();
  assert.equal(env.requests[0].payload.messages[0].attachments.length, 6);
  assert.equal(env.requests[0].payload.messages[0].attachments[0].size, 3);
  assert.ok(env.notices.some((notice) => /最多添加 6/.test(notice.message)));
});

test('lost IndexedDB bodies are marked unavailable while the conversation remains usable', async () => {
  const env = await environment(); await env.addFiles([file()]); await env.send();
  const reloaded = await environment({ storage: env.storage, attachments: new Map() });
  await reloaded.send('continue without the file');
  assert.match(reloaded.requests[0].payload.messages[0].content, /Attachment content unavailable/);
  assert.match(reloaded.nodes.get('agentMessages').innerHTML, /内容未保存/);
  assert.equal(reloaded.current().messages.at(-1).content, 'final answer');
});


test('locale refresh updates every persistent chat shell label without replacing the draft or losing attachments and scroll', async () => {
  const env = await environment();
  await env.addFiles([file('draft.txt')]);
  const prompt = env.nodes.get('agentPrompt');
  const shell = env.nodes.get('chat');
  prompt.value = 'unfinished draft';
  env.nodes.get('agentMessages').scrollHeight = 1000;
  env.nodes.get('agentMessages').scrollTop = 37;
  env.nodes.get('agentMessages').listeners.scroll();
  env.setLocale('en');
  await env.page.load();
  env.page.renderHistory();
  assert.equal(env.nodes.get('chat'), shell);
  assert.equal(env.nodes.get('agentPrompt'), prompt);
  assert.equal(prompt.value, 'unfinished draft');
  assert.equal(prompt.getAttribute('placeholder'), 'Write a message…');
  assert.equal(prompt.getAttribute('aria-label'), 'Write a message…');
  assert.equal(env.nodes.get('agentMessages').scrollTop, 37);
  assert.match(env.nodes.get('agentAttachmentStrip').innerHTML, /draft.txt/);
  assert.equal(env.actions.get('new').getAttribute('title'), 'New chat');
  assert.equal(env.actions.get('new').getAttribute('aria-label'), 'New chat');
  assert.equal(env.actions.get('attachment-menu').getAttribute('aria-label'), 'Add content');
  assert.equal(env.actions.get('history').getAttribute('aria-label'), 'Recent');
  assert.equal(env.nodes.get('chatCurrentTitle').textContent, 'Recent');
  assert.equal(env.actions.get('details').getAttribute('aria-label'), 'Conversation settings');
  assert.equal(env.nodes.get('agentSend').getAttribute('aria-label'), 'Send message');
  assert.equal(env.nodes.get('agentStop').getAttribute('aria-label'), 'Stop generating');
  for (const id of ['agentDrawerBackdrop', 'agentHistoryDrawer-close', 'agentDetailsDrawer-close']) assert.equal(env.nodes.get(id).getAttribute('aria-label'), 'Close');
  assert.equal(env.nodes.get('chatAddPhotoLabel').textContent, 'Add photos');
  assert.equal(env.nodes.get('chatAddFileLabel').textContent, 'Add files');
  assert.equal(env.nodes.get('chatAddFileHint').textContent, 'Text, code, PDF, or DOCX');
  assert.equal(env.nodes.get('agentDetailsTitle').textContent, 'Conversation settings');
  assert.match(env.nodes.get('chatComposerHint').textContent, /Enter to send/);
  await env.send('use draft attachment');
  assert.equal(env.requests[0].payload.messages[0].attachments[0].content, 'notes');
});

function historyStorage(items, active = items[0].id) {
  return new Map([['rotor.chat.conversations.v1', JSON.stringify(items.map((item) => ({
    model: 'model-a', messages: [], draft: { text: '', attachments: [] }, updatedAt: '2026-01-01', ...item,
  })))], ['rotor.chat.active.v1', active]]);
}

test('new chat reuses only truly empty conversations and inherits a separate settings object', async () => {
  const env = await environment();
  const original = env.current().id;
  env.change('chatSystemPrompt', 'Inherited'); env.change('chatApiFormat', 'responses');
  await env.page.onClick(env.actions.get('new'));
  assert.equal(env.current().id, original);
  env.change('agentPrompt', 'Keep this draft');
  await env.page.onClick(env.actions.get('new'));
  const second = env.current().id; assert.notEqual(second, original);
  assert.equal(env.current().settings.system_prompt, 'Inherited');
  env.change('chatSystemPrompt', 'Only second');
  await env.page.onClick(env.actions.get('new')); assert.equal(env.current().id, second);
  await env.page.onClick(env.target('open', { conversationId: original }));
  assert.equal(env.nodes.get('agentPrompt').value, 'Keep this draft');
  assert.equal(env.current().settings.system_prompt, 'Inherited');
  await env.page.onClick(env.actions.get('new'));
  assert.equal(env.current().id, second);
  assert.equal(JSON.parse(env.storage.get('rotor.chat.conversations.v1')).length, 2);
});

test('draft text, attachment payloads and reading positions survive switches and draft data survives reload', async () => {
  const env = await environment();
  const first = env.current().id;
  env.change('agentPrompt', 'Draft one'); await env.addFiles([file('one.txt', 'one')]);
  const messages = env.nodes.get('agentMessages'); messages.scrollHeight = 1600; messages.clientHeight = 300; messages.scrollTop = 180; messages.listeners.scroll();
  await env.page.onClick(env.actions.get('new'));
  const second = env.current().id;
  env.change('agentPrompt', 'Draft two'); await env.addFiles([file('two.txt', 'two')]);
  await env.page.onClick(env.target('open', { conversationId: first }));
  assert.equal(env.nodes.get('agentPrompt').value, 'Draft one'); assert.equal(messages.scrollTop, 180);
  assert.match(env.nodes.get('agentAttachmentStrip').innerHTML, /one.txt/); assert.doesNotMatch(env.nodes.get('agentAttachmentStrip').innerHTML, /two.txt/);
  assert.equal(env.attachments.size, 2);
  env.page.unload(); await env.page.load();
  assert.equal(env.nodes.get('agentPrompt').value, 'Draft one'); assert.equal(env.attachments.size, 2);
  const reload = await environment({ storage: env.storage, attachments: env.attachments });
  assert.equal(reload.nodes.get('agentPrompt').value, 'Draft one');
  await reload.send('use first'); assert.equal(reload.requests[0].payload.messages.at(-1).attachments[0].content, 'one');
  await reload.page.onClick(reload.target('open', { conversationId: second }));
  assert.equal(reload.nodes.get('agentPrompt').value, 'Draft two');
  assert.match(reload.nodes.get('agentAttachmentStrip').innerHTML, /two.txt/);
});

test('history search filters titles and models without replacing its input or moving the reading position', async () => {
  const storage = historyStorage([
    { id: 'a', title: 'Design review', model: 'model-a', messages: [{ role: 'user', content: 'design' }] },
    { id: 'b', title: 'Debug issue', model: 'model-b', messages: [{ role: 'user', content: 'debug' }] },
  ]);
  const env = await environment({ storage });
  assert.equal(env.nodes.get('chatCurrentTitle').textContent, '最近');
  const scroller = env.nodes.get('agentMessages'); scroller.scrollHeight = 1200; scroller.scrollTop = 120; scroller.listeners.scroll();
  await env.page.onClick(env.actions.get('history'));
  const input = env.nodes.get('chatHistorySearch'); assert.equal(env.focused(), input);
  env.change('chatHistorySearch', 'MODEL-B');
  assert.match(env.nodes.get('agentThreadList').innerHTML, /Debug issue/); assert.doesNotMatch(env.nodes.get('agentThreadList').innerHTML, /Design review/);
  env.change('chatHistorySearch', 'missing'); assert.match(env.nodes.get('agentThreadList').innerHTML, /没有匹配/);
  assert.equal(env.nodes.get('chatHistorySearch'), input); assert.equal(scroller.scrollTop, 120);
  env.change('chatHistorySearch', 'design'); assert.match(env.nodes.get('agentThreadList').innerHTML, /Design review/);
  await env.page.onClick(env.target('open', { conversationId: 'b' }));
  assert.equal(env.nodes.get('agentHistoryDrawer').classList.contains('hidden'), true);
  assert.equal(env.nodes.get('chatCurrentTitle').textContent, '最近'); assert.equal(input.value, '');
});

for (const viewportWidth of [1200, 375]) {
  test(`opening settings/history does not scroll and the first outside send works at ${viewportWidth}px`, async () => {
    const gate = deferred(); const env = await environment({ viewportWidth, streams: [{ gate }] });
    const messages = env.nodes.get('agentMessages'); messages.scrollHeight = 1200; messages.scrollTop = 100; messages.listeners.scroll();
    await env.page.onClick(env.actions.get('details'));
    await env.page.onClick(env.actions.get('details')); assert.equal(messages.scrollTop, 100);
    await env.page.onClick(env.actions.get('history')); assert.equal(messages.scrollTop, 100);
    assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
    const sending = env.send('send immediately');
    await env.page.onClick(env.nodes.get('agentSend')); await tick();
    assert.equal(env.nodes.get('agentHistoryDrawer').classList.contains('hidden'), true); assert.equal(env.requests.length, 1);
    gate.resolve(); await sending;
  });
}

test('deleting another conversation removes only its saved and draft attachments', async () => {
  const env = await environment();
  await env.addFiles([file('sent.txt')]); await env.send('first');
  const first = env.current().id; await env.addFiles([file('draft.txt')]);
  await env.page.onClick(env.actions.get('new'));
  env.change('agentPrompt', 'keep active draft'); await env.addFiles([file('keep.txt')]);
  await env.page.onClick(env.target('delete', { conversationId: first })); await env.page.onClick(env.target('confirm-delete'));
  assert.equal(env.attachments.size, 1); assert.equal([...env.attachments.values()][0].name, 'keep.txt');
  assert.equal(env.nodes.get('agentPrompt').value, 'keep active draft'); assert.match(env.nodes.get('agentAttachmentStrip').innerHTML, /keep.txt/);
});

test('late attachment restoration cannot overwrite another conversation or revive a removed draft attachment', async () => {
  const slow = deferred();
  const meta = { id: 'saved-file', kind: 'file', name: 'saved.txt', mimeType: 'text/plain', size: 4 };
  const storage = historyStorage([{ id: 'a', draft: { text: 'A', attachments: [meta] } }, { id: 'b', draft: { text: 'B', attachments: [] } }], 'b');
  const env = await environment({ storage, attachmentReads: () => slow.promise });
  const opening = env.page.onClick(env.target('open', { conversationId: 'a' })); await tick();
  await env.page.onClick(env.target('remove-attachment', { attachmentId: meta.id }));
  await env.page.onClick(env.target('open', { conversationId: 'b' }));
  env.change('agentPrompt', 'B edited'); await env.addFiles([file('new.txt')]);
  slow.resolve({ ...meta, content: 'late' }); await opening;
  assert.equal(env.current().id, 'b'); assert.equal(env.nodes.get('agentPrompt').value, 'B edited');
  assert.match(env.nodes.get('agentAttachmentStrip').innerHTML, /new.txt/); assert.doesNotMatch(env.nodes.get('agentAttachmentStrip').innerHTML, /saved.txt/);
  const savedA = JSON.parse(env.storage.get('rotor.chat.conversations.v1')).find((item) => item.id === 'a');
  assert.equal(savedA.draft.attachments.length, 0);
});

test('input and attachments added while preparing an earlier request remain as the next draft', async () => {
  const slow = deferred();
  const meta = { id: 'old-file', kind: 'file', name: 'old.txt', mimeType: 'text/plain', size: 3 };
  const storage = historyStorage([{ id: 'old', messages: [{ role: 'user', content: 'earlier', attachments: [meta] }] }]);
  const env = await environment({ storage, attachmentReads: () => slow.promise });
  const sending = env.send('submitted snapshot'); await tick();
  env.nodes.get('agentPrompt').scrollHeight = 120;
  env.change('agentPrompt', 'next draft\nsecond line'); await env.addFiles([file('next.txt')]);
  slow.resolve({ ...meta, content: 'old' }); await sending;
  assert.equal(env.requests[0].payload.messages.at(-1).content, 'submitted snapshot');
  assert.equal(env.requests[0].payload.messages.at(-1).attachments.length, 0);
  assert.equal(env.nodes.get('agentPrompt').value, 'next draft\nsecond line');
  assert.equal(env.nodes.get('agentPrompt').style.height, '120px');
  assert.equal(env.current().draft.text, 'next draft\nsecond line'); assert.equal(env.current().draft.attachments[0].name, 'next.txt');
});

for (const ending of ['done', 'error', 'stop']) {
  test(`stream deltas and ${ending} preserve an earlier reading position and offer return to bottom`, async () => {
    const first = deferred(), second = deferred();
    const env = await environment({ streams: [{ gates: [first, second], chunks: [
      'data: {"type":"delta","content":"partial"}\n\n',
      ending === 'error' ? 'data: {"type":"error","message":"interrupted"}\n\n' : 'data: {"type":"done","answer":"complete"}\n\n',
    ] }] });
    const sending = env.send(); await tick();
    const scroller = env.nodes.get('agentMessages'); scroller.scrollHeight = 1500; scroller.clientHeight = 300; scroller.scrollTop = 160; scroller.listeners.scroll();
    first.resolve(); await tick();
    assert.equal(scroller.scrollTop, 160); assert.equal(env.nodes.get('chatScrollBottom').classList.contains('hidden'), false);
    if (ending === 'stop') env.nodes.get('agentStop').listeners.click(); else second.resolve();
    await sending; assert.equal(scroller.scrollTop, 160);
    assert.equal(env.nodes.get('chatScrollBottom').classList.contains('hidden'), false);
    await env.page.onClick(env.actions.get('scroll-bottom'));
    assert.equal(scroller.lastScroll.behavior, 'smooth');
    assert.equal(env.nodes.get('chatScrollBottom').classList.contains('hidden'), true);
  });
}

test('streaming follows the bottom only while the reader stays there, and reduced motion skips animation', async () => {
  const first = deferred(), second = deferred(); let scroller;
  const env = await environment({ reducedMotion: true, streams: [{ gates: [first, second], onChunk() { scroller.scrollHeight += 60; }, chunks: [
    'data: {"type":"delta","content":"following"}\n\n', 'data: {"type":"done","answer":"finished"}\n\n',
  ] }] });
  const sending = env.send(); await tick(); scroller = env.nodes.get('agentMessages');
  scroller.scrollHeight = 1000; scroller.clientHeight = 300; scroller.scrollTop = 700; scroller.listeners.scroll();
  first.resolve(); await tick(); assert.equal(scroller.scrollTop, 1060);
  second.resolve(); await sending; assert.equal(scroller.scrollTop, 1120);
  scroller.scrollTop = 100; scroller.listeners.scroll();
  await env.page.onClick(env.actions.get('scroll-bottom'));
  assert.equal(scroller.lastScroll, undefined); assert.equal(scroller.scrollTop, 1120);
});


test('a changed reading position is checked before rendering even before the scroll event arrives', async () => {
  const gate = deferred(); const env = await environment({ streams: [{ gate }] });
  const sending = env.send(); await tick();
  const scroller = env.nodes.get('agentMessages'); scroller.scrollHeight = 1500; scroller.clientHeight = 300;
  scroller.scrollTop = 100;
  gate.resolve(); await sending;
  assert.equal(scroller.scrollTop, 100);
  assert.equal(env.nodes.get('chatScrollBottom').classList.contains('hidden'), false);
});

for (const visible of [true, false]) {
  test(`settings choice ${visible ? 'shown' : 'hidden'} survives history, chat actions, conversation changes, navigation and reload`, async () => {
    const preference = 'rotor.chat.settings.visible.v1';
    const gate = deferred();
    const env = await environment({ storage: new Map([[preference, String(visible)]]), streams: [{ gate }] });
    const assertChoice = () => {
      assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), !visible);
      assert.equal(env.actions.get('details').getAttribute('aria-expanded'), String(visible));
    };
    assertChoice();
    await env.page.onClick(env.outside()); assertChoice();
    env.change('agentPrompt', 'typed draft'); await env.page.onClick(env.nodes.get('agentPrompt')); assertChoice();
    const original = env.current().id;
    const sending = env.send('request'); await tick(); assertChoice();
    await env.page.onClick(env.actions.get('history'));
    assert.equal(env.nodes.get('agentHistoryDrawer').classList.contains('hidden'), false); assertChoice();
    await env.page.onClick(env.nodes.get('chatHistorySearch'));
    assert.equal(env.nodes.get('agentHistoryDrawer').classList.contains('hidden'), false);
    env.listeners.keydown({ key: 'Escape' });
    assert.equal(env.nodes.get('agentHistoryDrawer').classList.contains('hidden'), true); assertChoice();
    await env.page.onClick(env.actions.get('scroll-bottom')); assertChoice();
    env.nodes.get('agentStop').listeners.click(); await env.page.onClick(env.nodes.get('agentStop')); await sending; assertChoice();
    await env.page.onClick(env.actions.get('history'));
    await env.page.onClick(env.nodes.get('agentHistoryDrawer-close'));
    assert.equal(env.nodes.get('agentHistoryDrawer').classList.contains('hidden'), true); assertChoice();
    await env.page.onClick(env.actions.get('new')); const second = env.current().id; assertChoice();
    await env.page.onClick(env.target('open', { conversationId: original })); assertChoice();
    await env.page.onClick(env.target('delete', { conversationId: second })); await env.page.onClick(env.target('confirm-delete')); assertChoice();
    await env.page.onClick(env.target('delete', { conversationId: original })); await env.page.onClick(env.target('confirm-delete')); assertChoice();
    env.setLocale('en'); await env.page.load(); env.page.renderHistory(); assertChoice();
    env.page.unload(); await env.page.load(); assertChoice();
    const reloaded = await environment({ storage: env.storage });
    assert.equal(reloaded.nodes.get('agentDetailsDrawer').classList.contains('hidden'), !visible);
    await reloaded.page.onClick(reloaded.actions.get('history'));
    await reloaded.page.onClick(reloaded.nodes.get('agentDetailsDrawer-close'));
    assert.equal(reloaded.nodes.get('agentDetailsDrawer').classList.contains('hidden'), true);
    assert.equal(reloaded.nodes.get('agentHistoryDrawer').classList.contains('hidden'), false);
    assert.equal(reloaded.storage.get(preference), 'false');
    await reloaded.page.onClick(reloaded.actions.get('details'));
    assert.equal(reloaded.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
    assert.equal(reloaded.storage.get(preference), 'true');
    const shownAgain = await environment({ storage: reloaded.storage });
    assert.equal(shownAgain.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
  });
}

test('settings default to shown for missing, invalid or unreadable preferences and remain usable when storage writes fail', async () => {
  const preference = 'rotor.chat.settings.visible.v1';
  for (const stored of [undefined, 'invalid']) {
    const env = await environment({ storage: stored === undefined ? new Map() : new Map([[preference, stored]]) });
    assert.equal(env.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
  }
  const unreadable = await environment({ storageReadFailure: true });
  assert.equal(unreadable.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
  const unwritable = await environment({ storageFailure: true });
  assert.equal(unwritable.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
  await unwritable.page.onClick(unwritable.actions.get('details'));
  assert.equal(unwritable.nodes.get('agentDetailsDrawer').classList.contains('hidden'), true);
  await unwritable.page.load(); unwritable.page.unload(); await unwritable.page.load();
  assert.equal(unwritable.nodes.get('agentDetailsDrawer').classList.contains('hidden'), true);
  await unwritable.page.onClick(unwritable.actions.get('details'));
  assert.equal(unwritable.nodes.get('agentDetailsDrawer').classList.contains('hidden'), false);
});
