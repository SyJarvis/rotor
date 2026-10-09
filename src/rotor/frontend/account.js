import { api } from "./api.js?v=3";
import { t } from "./i18n.js";
import { actor, setActor, requestLogout } from "./session.js?v=1";

export function initAccount() {
  const get = (id) => document.getElementById(id);
  let profile = null, dirty = false, pending = false, epoch = 0;
  const status = (message, error = false) => {
    get("accountStatus").textContent = message;
    get("accountStatus").classList.toggle("error", error);
  };
  function render() {
    get("accountUsername").textContent = profile?.username || actor()?.username || "";
    get("accountRole").textContent = t((profile?.role || actor()?.role) === "admin" ? "adminRole" : "userRole");
    get("accountSave").disabled = pending || !dirty;
    get("accountPasswordSave").disabled = pending;
  }
  async function load() {
    render();
    if (dirty || pending) return;
    const request = ++epoch;
    try {
      const result = await api("/api/me");
      if (request !== epoch || dirty) return;
      profile = result;
      get("accountDisplayName").value = result.display_name || "";
      render();
    } catch (error) { if (request === epoch) status(error.message, true); }
  }
  get("accountDisplayName").addEventListener("input", () => { dirty = get("accountDisplayName").value !== (profile?.display_name || ""); render(); });
  get("accountSave").addEventListener("click", async () => {
    if (pending || !dirty) return;
    pending = true; render();
    const name = get("accountDisplayName").value, request = epoch;
    try {
      const result = await api("/api/me", { method: "PATCH", body: JSON.stringify({ display_name: name }) });
      if (request !== epoch) return;
      profile = result;
      setActor({ ...actor(), ...profile });
      dirty = get("accountDisplayName").value !== name;
      status(t("updated"));
    } catch (error) { if (request === epoch) status(error.message, true); }
    finally { pending = false; render(); }
  });
  get("accountPasswordSave").addEventListener("click", async () => {
    if (pending) return;
    const currentPassword = get("accountCurrentPassword").value;
    const newPassword = get("accountNewPassword").value;
    if (newPassword.length < 6) { status(t("passwordMinimum"), true); return; }
    if (newPassword !== get("accountConfirmPassword").value) { status(t("passwordMismatch"), true); return; }
    pending = true; render();
    const request = epoch;
    try {
      const result = await api("/api/auth/change-password", { method: "POST", body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) });
      if (request !== epoch) return;
      setActor({ ...actor(), ...result });
      for (const id of ["accountCurrentPassword", "accountNewPassword", "accountConfirmPassword"]) get(id).value = "";
      status(t("passwordUpdated"));
    } catch (error) { if (request === epoch) status(error.message, true); }
    finally { pending = false; render(); }
  });
  get("accountLogout").addEventListener("click", requestLogout);
  document.addEventListener("sessionended", () => { ++epoch; profile = null; dirty = false; get("accountUsername").textContent = ""; for (const id of [ "accountDisplayName", "accountCurrentPassword", "accountNewPassword", "accountConfirmPassword"]) get(id).value = ""; });
  document.addEventListener("localechange", render);
  return { load };
}
