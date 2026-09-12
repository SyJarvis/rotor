const SESSION_ENDPOINT = "/api/admin/auth/me";
const LOGIN_ENDPOINT = "/api/admin/auth/login";
const CHANGE_PASSWORD_ENDPOINT = "/api/admin/auth/change-password";
const LOGOUT_ENDPOINT = "/api/admin/auth/logout";

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
            <input name="username" autocomplete="username" value="admin" required>
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
  panel.querySelector("input")?.focus();
}

function showLogin(message = "") {
  showPanel("adminLoginPanel", message);
}

function showPasswordChange(message = "") {
  showPanel("adminPasswordPanel", message);
}

async function startApp(actor) {
  if (actor.must_change_password) {
    showPasswordChange();
    return;
  }
  authScreen().classList.add("hidden");
  document.body.classList.remove("auth-pending");
  if (!appLoaded) {
    appLoaded = true;
    await import("./app.js?v=33");
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

async function logout() {
  const csrf = csrfToken();
  await fetch(LOGOUT_ENDPOINT, {
    method: "POST",
    credentials: "same-origin",
    headers: csrf ? { "X-CSRF-Token": csrf } : {},
  });
  window.location.reload();
}

window.addEventListener("rotorauthrequired", () => {
  showLogin(labels().adminSessionExpired);
});
document.getElementById("logoutBtn")?.addEventListener("click", logout);

try {
  const response = await fetch(SESSION_ENDPOINT, {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.ok) await startApp(await response.json());
  else showLogin();
} catch {
  showLogin(labels().adminServiceUnavailable);
}
