import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../src/rotor/frontend/pages/monitoring.js', import.meta.url), 'utf8');
const translations = await readFile(new URL('../src/rotor/frontend/config.js', import.meta.url), 'utf8');

for (const language of ['zh-CN', 'en']) {
  test(`client requests display without active sessions (${language})`, async () => {
    const content = { innerHTML: '', setAttribute() {} };
    const buttons = ['performance', 'sources'].map((name) => ({
      dataset: { monitoringTab: name }, setAttribute() {},
    }));
    const root = { querySelectorAll: () => buttons };
    const window = {};
    vm.runInNewContext(translations, { window });
    const context = vm.createContext({ document: {
      getElementById: (id) => id === 'monitoring' ? root : content,
    } });
    const bindings = {
      api: async () => ({ agents: [{ id: 'pi', name: 'Pi', request_count: 2,
        ungrouped_request_count: 2, session_count: 0, active_session_count: 0,
        total_tokens: 123, turn_count: 0 }] }),
      t: (key) => window.ROTOR_UI_CONFIG.locales[language][key] ?? key,
      escapeHtml: (value) => String(value ?? ''),
      formatRequestCount: (value) => String(value ?? 0),
      formatTimeInTimezone: () => '—', refreshIcons() {},
    };
    // Select the sources tab while keeping the production renderer unchanged.
    const module = new vm.SourceTextModule(source.replace('let selected = "performance"', 'let selected = "sources"'), { context });
    await module.link((specifier) => {
      const names = specifier.includes('api.js') ? ['api'] : specifier.includes('i18n.js') ? ['t']
        : specifier.includes('performance.js') ? ['load', 'unload']
          : ['escapeHtml', 'formatRequestCount', 'formatTimeInTimezone', 'refreshIcons'];
      return new vm.SyntheticModule(names, function () {
        names.forEach((name) => this.setExport(name, bindings[name] ?? (() => {})));
      }, { context });
    });
    await module.evaluate();
    await module.namespace.load();
    assert.match(content.innerHTML, /Pi/);
    assert.match(content.innerHTML, language === 'en' ? /Requests received today/ : /今日有请求/);
    assert.match(content.innerHTML, language === 'en' ? /Requests Without Session ID/ : /无会话 ID 请求/);
    assert.match(content.innerHTML, /<strong>2<\/strong>/);
    assert.match(content.innerHTML, /<strong>0<\/strong>/);
    assert.doesNotMatch(content.innerHTML, /undefined|NaN|todayRequests|ungroupedRequests/);
  });
}
