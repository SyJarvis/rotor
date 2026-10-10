import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const authSource = await readFile(new URL('../src/rotor/frontend/auth.js', import.meta.url), 'utf8');
const configSource = await readFile(new URL('../src/rotor/frontend/config.js', import.meta.url), 'utf8');

async function environment({ networkFails = false, status = 200, importFails = false, language = 'zh-CN', mustChangePassword = false, href = 'http://localhost/', registrationError = null } = {}) {
  const nodes = new Map(); let activeElement;
  function element(id = '') {
    const classes = new Set(), listeners = new Map();
    const node = {
      id, textContent: '', children: [], disabled: false, value: '', elements: {}, resetCount: 0,
      classList: { add: (value) => classes.add(value), remove: (value) => classes.delete(value), contains: (value) => classes.has(value), toggle(value, yes) { yes ? classes.add(value) : classes.delete(value); } },
      appendChild(child) { this.children.push(child); nodes.set(child.id, child); },
      addEventListener(name, callback) { listeners.set(name, callback); },
      click() { return listeners.get('click')?.({ currentTarget: this, preventDefault() {} }); },
      submit() { return listeners.get('submit')?.({ currentTarget: this, preventDefault() {} }); },
      reset() { this.resetCount++; for (const input of Object.values(this.elements)) input.value = ''; },
      focus() { activeElement = this; },
      querySelector(selector) {
        if (selector.startsWith('#')) return nodes.get(selector.slice(1));
        if (selector === 'button[type=submit]') return this.submitButton;
        if (selector === '[data-auth-error]') return this.error;
        if (selector === 'input, button') return this.firstInput || this.firstButton;
        return null;
      },
      querySelectorAll(selector) { return selector === '.auth-card > section' ? this.children : []; },
      set innerHTML(html) {
        this.markup = html;
        for (const match of html.matchAll(/<section id="([^"]+)"[^>]*>([\s\S]*?)<\/section>/g)) {
          const panel = element(match[1]); panel.error = element(); let form;
          for (const tag of match[2].matchAll(/<(form|input|button|a)\b([^>]*)>/g)) {
            const id = tag[2].match(/\bid="([^"]+)"/)?.[1] || '';
            const child = element(id);
            if (tag[1] === 'form') form = child;
            if (tag[1] === 'input' && form) form.elements[tag[2].match(/\bname="([^"]+)"/)?.[1]] = child;
            if (tag[1] === 'button' && form && /type="submit"/.test(tag[2])) form.submitButton = child;
            if (tag[1] === 'input' && !panel.firstInput) panel.firstInput = child;
            if (tag[1] === 'button' && !panel.firstButton) panel.firstButton = child;
            if (id) nodes.set(id, child);
          }
          this.appendChild(panel);
        }
      },
    };
    return node;
  }
  const body = element('body'); body.classList.add('auth-pending');
  const document = { cookie: 'rotor_admin_session=existing; rotor_admin_csrf=csrf', body,
    createElement: () => element(), getElementById: (id) => nodes.get(id) || null,
  };
  let reloads = 0, imports = 0, failImport = importFails;
  const importPaths = [];
  const window = { addEventListener() {}, location: { href, pathname: new URL(href).pathname, reload() { reloads++; } } };
  window.history = { replaceState(_state, _title, url) { window.cleanUrl = url; window.location.href = new URL(url, window.location.href).href; } };
  vm.runInNewContext(configSource, { window });
  const requests = [];
  const context = vm.createContext({ document, window, URL, FormData: class { constructor(form) { this.form = form; } get(name) { return this.form.elements[name]?.value; } }, localStorage: { getItem: () => language },
    fetch: async (path, options) => {
      requests.push({ path, options });
      if (networkFails) throw new TypeError('Failed to fetch');
      if (path === '/api/auth/register' && registrationError) return { ok: false, status: 400, json: async () => ({ detail: registrationError }) };
      if (path === '/api/auth/register') return { ok: true, json: async () => ({ id: 8, username: 'new-user', role: 'user', must_change_password: false }) };
      if (path === '/api/auth/login' || path === '/api/auth/change-password') return { ok: true, json: async () => ({ id: 7, username: 'test-user', role: 'user', must_change_password: false }) };
      return { ok: status === 200, status, json: async () => ({ id: 7, username: 'test-user', role: 'user', must_change_password: mustChangePassword }) };
    },
  });
  const module = new vm.SourceTextModule(authSource + '\nexport { appLoaded, startApp, submitRegister };', { context,
    importModuleDynamically: async (specifier) => {
      importPaths.push(specifier);
      imports++; window.pendingDuringImport = body.classList.contains("auth-pending");
      if (failImport) throw new SyntaxError("The requested module './i18n.js' does not provide an export named 'setLocale'");
      const app = new vm.SyntheticModule([], () => {}, { context }); await app.link(() => {}); await app.evaluate(); return app;
    },
  });
  await module.link(() => new vm.SyntheticModule(['setActor', 'endSession'], function () { this.setExport('setActor', (value) => { window.ROTOR_ACTOR = value; }); this.setExport('endSession', () => { window.ROTOR_ACTOR = null; }); }, { context })); await module.evaluate();
  return { module, document, window, requests, importPaths, get: (id) => nodes.get(id), get imports() { return imports; }, get reloads() { return reloads; }, get activeElement() { return activeElement; }, allowImport() { failImport = false; }, allowRegistration() { registrationError = null; } };
}

test('module startup failure is distinct from service failure, keeps loaded false, and offers reload without clearing session', async () => {
  for (const language of ['zh-CN', 'en']) {
    const h = await environment({ importFails: true, language });
    assert.equal(h.module.namespace.appLoaded, false); assert.equal(h.imports, 1);
    assert.equal(h.get('adminStartupPanel').classList.contains('hidden'), false);
    assert.equal(h.get('adminLoginPanel').classList.contains('hidden'), true);
    assert.match(h.get('adminStartupPanel').error.textContent, language === 'en' ? /page resources could not load/ : /页面资源未能加载/);
    assert.doesNotMatch(h.get('adminStartupPanel').error.textContent, /Unable to reach|无法连接/);
    assert.equal(h.activeElement, h.get('adminReloadApp'));
    h.get('adminReloadApp').click(); assert.equal(h.reloads, 1);
    assert.equal(h.document.cookie, 'rotor_admin_session=existing; rotor_admin_csrf=csrf');
    h.allowImport(); await h.module.namespace.startApp({ must_change_password: false });
    assert.equal(h.imports, 2); assert.equal(h.module.namespace.appLoaded, true);
    assert.equal(h.get('adminAuthScreen').classList.contains('hidden'), true); assert.equal(h.document.body.classList.contains('auth-pending'), false);
    await h.module.namespace.startApp({ must_change_password: false }); assert.equal(h.imports, 2);
  }
});

test('a real session fetch network failure retains the service-unavailable message and never imports app', async () => {
  const h = await environment({ networkFails: true });
  assert.equal(h.get('adminLoginPanel').classList.contains('hidden'), false);
  assert.equal(h.get('adminLoginPanel').error.textContent, '无法连接 Rotor 管理服务');
  assert.equal(h.imports, 0); assert.equal(h.module.namespace.appLoaded, false);
  assert.equal(h.requests[0].path, '/api/auth/me'); assert.equal(h.requests[0].options.cache, 'no-store');
});

test('unauthenticated and password-change-required sessions retain their existing flows without importing app', async () => {
  const anonymous = await environment({ status: 401 });
  assert.equal(anonymous.get('adminLoginPanel').classList.contains('hidden'), false);
  assert.equal(anonymous.get('adminLoginPanel').error.textContent, ''); assert.equal(anonymous.imports, 0);
  const reset = await environment({ mustChangePassword: true });
  assert.equal(reset.get('adminPasswordPanel').classList.contains('hidden'), false); assert.equal(reset.imports, 0);
});

function fillRegistration(h, values = {}) {
  const form = h.get('registerForm');
  const defaults = { invite_token: '  manually-entered-code  ', username: 'new-user', password: 'test-password', confirm_password: 'test-password' };
  for (const [name, value] of Object.entries({ ...defaults, ...values })) form.elements[name].value = value;
  return form;
}

test('login offers a standalone registration page and submits the manually entered code without a role or code in the URL', async () => {
  const h = await environment({ status: 401 });
  assert.equal(h.get('adminLoginPanel').classList.contains('hidden'), false);
  h.get('openRegistration').click();
  assert.equal(h.window.cleanUrl, '/?register=1');
  assert.equal(h.get('registerPanel').classList.contains('hidden'), false);
  assert.doesNotMatch(h.get('adminAuthScreen').markup, /value="admin"|name="role"/);
  const form = fillRegistration(h);
  assert.deepEqual(Object.keys(form.elements).sort(), ['confirm_password', 'invite_token', 'password', 'username']);
  await form.submit();
  const request = h.requests.find(({ path }) => path === '/api/auth/register');
  assert.deepEqual(JSON.parse(request.options.body), { invite_token: 'manually-entered-code', username: 'new-user', password: 'test-password' });
  assert.equal(h.window.ROTOR_ACTOR.role, 'user'); assert.equal(h.window.cleanUrl, '/'); assert.equal(form.resetCount, 1);
  assert.equal(h.window.pendingDuringImport, true); assert.equal(h.document.body.classList.contains('auth-pending'), false);
});

test('direct registration refresh and login/register navigation retain the selected page and in-page draft', async () => {
  const h = await environment({ status: 401, href: 'http://localhost/?register=1' });
  assert.equal(h.get('registerPanel').classList.contains('hidden'), false);
  assert.equal(h.get('registerForm').elements.invite_token.value, '');
  const form = fillRegistration(h);
  h.get('backToLogin').click();
  assert.equal(h.window.cleanUrl, '/'); assert.equal(h.get('adminLoginPanel').classList.contains('hidden'), false);
  const refreshedLogin = await environment({ status: 401, href: h.window.location.href });
  assert.equal(refreshedLogin.get('adminLoginPanel').classList.contains('hidden'), false);
  h.get('openRegistration').click();
  assert.equal(h.get('registerForm'), form); assert.equal(form.elements.username.value, 'new-user');
  assert.equal(form.elements.invite_token.value, '  manually-entered-code  ');
  const refreshedRegister = await environment({ status: 401, href: h.window.location.href });
  assert.equal(refreshedRegister.get('registerPanel').classList.contains('hidden'), false);
  assert.equal(refreshedRegister.get('registerForm').elements.invite_token.value, '');
});

test('missing invitation code, short password and confirmation mismatch block registration and retain input', async () => {
  for (const language of ['zh-CN', 'en']) {
    const h = await environment({ status: 401, href: 'http://localhost/?register=1', language });
    for (const [values, message] of [
      [{ invite_token: '   ' }, language === 'en' ? 'Enter an invitation code.' : '请输入邀请码。'],
      [{ password: 'short', confirm_password: 'short' }, language === 'en' ? 'Use at least 6 characters for the password.' : '密码至少需要 6 个字符。'],
      [{ confirm_password: 'different' }, language === 'en' ? 'The passwords do not match.' : '两次输入的密码不一致。'],
    ]) {
      const form = fillRegistration(h, values); await form.submit();
      assert.equal(h.get('registerPanel').error.textContent, message);
      assert.equal(form.elements.username.value, 'new-user'); assert.equal(form.resetCount, 0);
      assert.equal(form.submitButton.disabled, false);
    }
    assert.equal(h.requests.filter(({ path }) => path === '/api/auth/register').length, 0);
  }
});

test('rejected registration preserves all fields and allows replacing the invitation code before retry', async () => {
  const h = await environment({ status: 401, href: 'http://localhost/?register=1', registrationError: 'Invitation code expired' });
  const form = fillRegistration(h); const before = Object.fromEntries(Object.entries(form.elements).map(([name, input]) => [name, input.value]));
  await form.submit();
  assert.equal(h.get('registerPanel').error.textContent, 'Invitation code expired');
  assert.deepEqual(Object.fromEntries(Object.entries(form.elements).map(([name, input]) => [name, input.value])), before);
  assert.equal(form.resetCount, 0); assert.equal(form.submitButton.disabled, false); assert.equal(h.imports, 0);
  form.elements.invite_token.value = ' replacement-code '; h.allowRegistration(); await form.submit();
  const registrations = h.requests.filter(({ path }) => path === '/api/auth/register');
  assert.equal(JSON.parse(registrations[1].options.body).invite_token, 'replacement-code');
  assert.equal(h.window.ROTOR_ACTOR.role, 'user'); assert.equal(form.resetCount, 1);
});

const oauthQuery = 'response_type=code&client_id=coding-agent&redirect_uri=http%3A%2F%2Flocalhost%3A8123%2Fcallback&state=state%2Bvalue&code_challenge=challenge&code_challenge_method=S256&scope=inference';

test('authorization loads its standalone module while the console retains its app module', async () => {
  for (const path of ['/oauth/authorize']) {
    const h = await environment({ href: `http://localhost${path}?${oauthQuery}` });
    assert.deepEqual(h.importPaths, ['./client-authorization.js?v=2']);
    assert.equal(h.document.body.classList.contains('auth-pending'), false);
  }
  const console = await environment();
  assert.deepEqual(console.importPaths, ['./app.js?v=67']);
});

test('authorization parameters survive registration switching, registration completion and page refresh', async () => {
  const href = `http://localhost/oauth/authorize?${oauthQuery}`;
  const h = await environment({ status: 401, href });
  const registrationLink = new URL(h.get('openRegistration').href, href);
  assert.equal(registrationLink.searchParams.get('state'), 'state+value');
  assert.equal(registrationLink.searchParams.get('register'), '1');
  h.get('openRegistration').click();
  const registeredUrl = new URL(h.window.location.href);
  assert.equal(registeredUrl.searchParams.get('register'), '1');
  assert.equal(registeredUrl.searchParams.get('state'), 'state+value');
  const refreshed = await environment({ status: 401, href: registeredUrl.href });
  assert.equal(refreshed.get('registerPanel').classList.contains('hidden'), false);
  h.get('backToLogin').click();
  assert.equal(h.window.location.href, href);
  h.get('openRegistration').click();
  await fillRegistration(h).submit();
  assert.equal(h.window.location.href, href);
  assert.deepEqual(h.importPaths, ['./client-authorization.js?v=2']);
});

test('authorization parameters survive ordinary login and required password change', async () => {
  const href = `http://localhost/oauth/authorize?${oauthQuery}`;
  const login = await environment({ status: 401, href });
  const loginForm = login.get('adminLoginForm');
  loginForm.elements.username.value = 'test-user'; loginForm.elements.password.value = 'test-password';
  await loginForm.submit();
  assert.equal(login.window.location.href, href);
  assert.deepEqual(login.importPaths, ['./client-authorization.js?v=2']);
  const reset = await environment({ mustChangePassword: true, href });
  const resetForm = reset.get('adminPasswordForm');
  resetForm.elements.current_password.value = 'old-password';
  resetForm.elements.new_password.value = 'new-password'; resetForm.elements.confirm_password.value = 'new-password';
  await resetForm.submit();
  assert.equal(reset.window.location.href, href);
  assert.deepEqual(reset.importPaths, ['./client-authorization.js?v=2']);
});

test('security deep link survives sign-in and registration while root registration keeps its clean URL', async () => {
  const href = 'http://localhost/?settings=security&other=preserved#anchor';
  const h = await environment({ status: 401, href });
  const link = new URL(h.get('openRegistration').href, href);
  assert.equal(link.searchParams.get('settings'), 'security'); assert.equal(link.hash, '#anchor');
  h.get('openRegistration').click(); await fillRegistration(h).submit();
  assert.equal(h.window.location.href, href); assert.deepEqual(h.importPaths, ['./app.js?v=67']);
  const login = await environment({ status: 401, href });
  login.get('adminLoginForm').elements.username.value = 'test-user'; login.get('adminLoginForm').elements.password.value = 'test-password';
  await login.get('adminLoginForm').submit(); assert.equal(login.window.location.href, href);
});
