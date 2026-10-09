import { setActor, endSession } from "./session.js?v=1";

const SESSION_ENDPOINT = "/api/auth/me";
const LOGIN_ENDPOINT = "/api/auth/login";
const CHANGE_PASSWORD_ENDPOINT = "/api/auth/change-password";
const LOGOUT_ENDPOINT = "/api/auth/logout";
const registrationRequested = new URL(window.location.href).searchParams.get("register") === "1";

let appLoaded = false;

function labels() {
  let locale = window.ROTOR_UI_CONFIG.defaultLocale;
  try {
    locale = localStorage.getItem("rotor.locale") || locale;
  } catch {}
  return window.ROTOR_UI_CONFIG.locales[locale]
    || window.ROTOR_UI_CONFIG.locales[window.ROTOR_UI_CONFIG.defaultLocale];
}

function csrfToken() {
  const prefix = "rotor_admin_csrf=";
  const value = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix));
  return value ? decodeURIComponent(value.slice(prefix.length)) : "";
}

function authScreen() {
  let screen = document.getElementById("adminAuthScreen");
  if (screen) return screen;

  const text = labels();
  screen = document.createElement("div");
  screen.id = "adminAuthScreen";
  screen.className = "auth-screen hidden";
  screen.innerHTML = `
    <main class="auth-card">
      <div class="auth-brand-mark" aria-hidden="true">◆</div>
      <section id="adminLoginPanel">
        <h1>${text.adminLoginTitle}</h1>
        <p>${text.adminLoginDescription}</p>
        <form id="adminLoginForm">
          <label>
            <span>${text.adminUsername}</span>
            <input name="username" autocomplete="username" required>
          </label>
          <label>
            <span>${text.adminPassword}</span>
            <input
              name="password"
              type="password"
              autocomplete="current-password"
              required
            >
          </label>
          <p class="auth-error hidden" data-auth-error role="alert"></p>
          <button type="submit" class="btn-primary">${text.signIn}</button>
        </form>
        <a href="?register=1" class="auth-switch" id="openRegistration">${text.inviteRegister}</a>
      </section>
      <section id="registerPanel" class="hidden">
        <h1>${text.inviteRegister}</h1><p>${text.inviteOnly}</p>
        <form id="registerForm">
          <label><span>${text.invitationCode}</span><input name="invite_token" autocomplete="off" autocapitalize="none" spellcheck="false" required></label>
          <label><span>${text.adminUsername}</span><input name="username" autocomplete="username" required></label>
          <label><span>${text.adminPassword}</span><input name="password" type="password" autocomplete="new-password" minlength="6" required></label>
          <label><span>${text.registerConfirmPassword}</span><input name="confirm_password" type="password" autocomplete="new-password" minlength="6" required></label>
          <p class="auth-error hidden" data-auth-error role="alert"></p>
          <button type="submit" class="btn-primary">${text.inviteRegister}</button>
          <button type="button" class="btn-secondary" id="backToLogin">${text.backToLogin}</button>
        </form>
      </section>
      <section id="adminStartupPanel" class="hidden">
        <h1>${text.adminAppLoadFailed}</h1>
        <p class="auth-error" data-auth-error role="alert"></p>
        <button type="button" class="btn-primary" id="adminReloadApp">${text.adminReloadApp}</button>
      </section>
      <section id="adminPasswordPanel" class="hidden">
        <h1>${text.changePasswordTitle}</h1>
        <p>${text.changePasswordDescription}</p>
        <form id="adminPasswordForm">
          <label>
            <span>${text.currentPassword}</span>
            <input
              name="current_password"
              type="password"
              autocomplete="current-password"
              required
            >
          </label>
          <label>
            <span>${text.newPassword}</span>
            <input
              name="new_password"
              type="password"
              autocomplete="new-password"
              minlength="6"
              required
            >
          </label>
          <label>
            <span>${text.confirmPassword}</span>
            <input
              name="confirm_password"
              type="password"
              autocomplete="new-password"
              minlength="6"
              required
            >
          </label>
          <p class="auth-error hidden" data-auth-error role="alert"></p>
          <button type="submit" class="btn-primary">${text.savePassword}</button>
        </form>
      </section>
    </main>
  `;
  document.body.appendChild(screen);
  screen.querySelector("#adminLoginForm")
    ?.addEventListener("submit", submitLogin);
  screen.querySelector("#adminPasswordForm")
    ?.addEventListener("submit", submitPasswordChange);
  screen.querySelector("#registerForm")?.addEventListener("submit", submitRegister);
  screen.querySelector("#openRegistration")?.addEventListener("click", (event) => { event.preventDefault(); switchAuthPage(true); });
  screen.querySelector("#backToLogin")?.addEventListener("click", () => switchAuthPage(false));
  screen.querySelector("#adminReloadApp")
    ?.addEventListener("click", () => window.location.reload());
  return screen;
}

function showPanel(panelId, message = "") {
  document.body.classList.add("auth-pending");
  const screen = authScreen();
  for (const panel of screen.querySelectorAll(".auth-card > section")) {
    panel.classList.toggle("hidden", panel.id !== panelId);
  }
  const panel = screen.querySelector(`#${panelId}`);
  const error = panel.querySelector("[data-auth-error]");
  error.textContent = message;
  error.classList.toggle("hidden", !message);
  screen.classList.remove("hidden");
  panel.querySelector("input, button")?.focus();
}

function showLogin(message = "") {
  showPanel("adminLoginPanel", message);
}

function switchAuthPage(register) {
  window.history.replaceState(null, "", `${window.location.pathname}${register ? "?register=1" : ""}`);
  showPanel(register ? "registerPanel" : "adminLoginPanel");
}

function showPasswordChange(message = "") {
  showPanel("adminPasswordPanel", message);
}

async function startApp(actor) {
  if (appLoaded) { window.location.reload(); return; }
  setActor(actor);
  if (actor.must_change_password) {
    showPasswordChange();
    return;
  }
  if (!appLoaded) {
    try {
      await import("./app.js?v=61");
      appLoaded = true;
      authScreen().classList.add("hidden");
      document.body.classList.remove("auth-pending");
    } catch {
      showPanel("adminStartupPanel", labels().adminAppLoadRetry);
    }
  }
}

async function submitLogin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  const values = new FormData(form);
  button.disabled = true;
  try {
    const response = await fetch(LOGIN_ENDPOINT, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: values.get("username"),
        password: values.get("password"),
      }),
    });
    if (!response.ok) {
      const message = response.status === 429
        ? labels().adminLoginRateLimited
        : labels().adminLoginFailed;
      throw new Error(message);
    }
    const actor = await response.json();
    form.reset();
    await startApp(actor);
  } catch (error) {
    showLogin(error.message || labels().adminLoginFailed);
  } finally {
    button.disabled = false;
  }
}

async function submitPasswordChange(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  const values = new FormData(form);
  const currentPassword = values.get("current_password");
  const newPassword = values.get("new_password");
  if (newPassword !== values.get("confirm_password")) {
    showPasswordChange(labels().passwordMismatch);
    return;
  }

  button.disabled = true;
  try {
    const response = await fetch(CHANGE_PASSWORD_ENDPOINT, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken(),
      },
      body: JSON.stringify({
        current_password: currentPassword,
        new_password: newPassword,
      }),
    });
    if (!response.ok) throw new Error(labels().passwordChangeFailed);
    const actor = await response.json();
    form.reset();
    await startApp(actor);
  } catch (error) {
    showPasswordChange(error.message || labels().passwordChangeFailed);
  } finally {
    button.disabled = false;
  }
}

async function submitRegister(event) {
  event.preventDefault();
  const form = event.currentTarget, button = form.querySelector("button[type=submit]");
  if (button.disabled) return;
  const values = new FormData(form);
  const inviteToken = String(values.get("invite_token") || "").trim();
  const password = String(values.get("password") || "");
  if (!inviteToken) { showPanel("registerPanel", labels().invitationCodeRequired); return; }
  if (password.length < 6) { showPanel("registerPanel", labels().passwordMinimum); return; }
  if (password !== values.get("confirm_password")) { showPanel("registerPanel", labels().registerPasswordMismatch); return; }
  button.disabled = true;
  try {
    const response = await fetch("/api/auth/register", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ invite_token: inviteToken, username: values.get("username"), password }) });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : labels().registrationFailed);
    form.reset();
    window.history.replaceState(null, "", window.location.pathname);
    await startApp(result);
  } catch (error) { showPanel("registerPanel", error.message || labels().registrationFailed); }
  finally { button.disabled = false; }
}

async function logout() {
  endSession();
  const csrf = csrfToken();
  await fetch(LOGOUT_ENDPOINT, {
    method: "POST",
    credentials: "same-origin",
    headers: csrf ? { "X-CSRF-Token": csrf } : {},
  });
  window.location.reload();
}

window.addEventListener("rotorauthrequired", () => {
  endSession();
  showLogin(labels().adminSessionExpired);
});
document.getElementById("logoutBtn")?.addEventListener("click", logout);
window.addEventListener("rotorlogout", logout);

try {
  const response = await fetch(SESSION_ENDPOINT, {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.ok) await startApp(await response.json());
  else if (registrationRequested) showPanel("registerPanel");
  else showLogin();
} catch {
  showPanel(registrationRequested ? "registerPanel" : "adminLoginPanel", labels().adminServiceUnavailable);
}
