import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../src/rotor/frontend/pages/channels.js', import.meta.url), 'utf8');
const translations = await readFile(new URL('../src/rotor/frontend/config.js', import.meta.url), 'utf8');

for (const [language, message, generic] of [
  ['zh-CN', '模型目录可访问（未验证生成）', '可访问'],
  ['en', 'Model catalog reachable (generation not verified)', 'reachable'],
]) {
  test(`channel catalog-only test is not presented as generation success (${language})`, async () => {
    const window = {};
    vm.runInNewContext(translations, { window });
    const locale = window.ROTOR_UI_CONFIG.locales[language];
    assert.equal(locale.reachable, generic);
    const elements = new Map();
    const getElement = (id) => {
      if (!elements.has(id)) elements.set(id, { innerHTML: '', value: '', addEventListener() {}, classList: { add() {}, remove() {} } });
      return elements.get(id);
    };
    const requests = [];
    const context = vm.createContext({ document: {
      getElementById: getElement, querySelector: () => null, addEventListener() {},
    } });
    const bindings = {
      api: async (url, options = {}) => {
        requests.push([url, options.method || 'GET']);
        if (url.endsWith('/test')) return { ok: true, models: ['local-model'], latency_ms: 3 };
        return [{ id: 1, name: 'channel', type: 'anthropic', protocol: 'anthropic', enabled: true, models: ['local-model'] }];
      },
      t: (key) => locale[key] ?? key,
      escapeHtml: (value) => String(value ?? ''),
    };
    const module = new vm.SourceTextModule(source, { context });
    await module.link((specifier) => {
      const names = specifier.includes('api.js') ? ['api'] : specifier.includes('i18n.js') ? ['t']
        : specifier.includes('channel-form.js') ? ['channelDefaults', 'rememberChannelDefaults', 'applyChannelDefaults']
          : ['escapeHtml', 'parseCsv', 'parseJson', 'badge', 'statusBadge', 'skeletonCards', 'showInline', 'hideInline', 'refreshIcons', 'toast'];
      return new vm.SyntheticModule(names, function () {
        names.forEach((name) => this.setExport(name, bindings[name] ?? (() => '')));
      }, { context });
    });
    await module.evaluate();
    await module.namespace.load();
    assert.equal(await module.namespace.onClick({ dataset: { channelTest: '1' }, closest: () => null }), true);
    assert.ok(getElement('channels').innerHTML.includes(message));
    assert.deepEqual(requests, [['/api/admin/channels', 'GET'], ['/api/admin/channels/1/test', 'POST']]);
  });
}
