import { api } from "./api.js?v=3";
import { t } from "./i18n.js";
import { actor, accountId, setActor } from "./session.js?v=1";

export function initAccount() {
  const get = (id) => document.getElementById(id);
  let profile = null, dirty = false, pending = false, epoch = 0, owner = accountId(), request = null;
  const status = (message = "", error = false) => {
    get("accountStatus").textContent = message;
    get("accountStatus").classList.toggle("error", error);
  };
  function render() {
    get("accountUsername").textContent = profile?.username || actor()?.username || "";
    get("accountRole").textContent = t((profile?.role || actor()?.role) === "admin" ? "adminRole" : "userRole");
    get("accountSave").disabled = pending || !dirty;
  }
  async function load() {
    render();
    if (dirty || pending) return;
    const version = ++epoch; request?.abort(); request = new AbortController();
    try {
      const result = await api("/api/me", { signal: request.signal });
      if (version !== epoch || dirty) return;
      profile = result;
      get("accountDisplayName").value = result.display_name || "";
      render();
    } catch (error) { if (version === epoch) status(error.message, true); }
  }
  get("accountDisplayName").addEventListener("input", () => { dirty = get("accountDisplayName").value !== (profile?.display_name || ""); render(); });
  get("accountSave").addEventListener("click", async () => {
    if (pending || !dirty) return;
    pending = true; render();
    const name = get("accountDisplayName").value, version = epoch;
    request?.abort(); request = new AbortController();
    try {
      const result = await api("/api/me", { method: "PATCH", signal: request.signal, body: JSON.stringify({ display_name: name }) });
      if (version !== epoch) return;
      profile = result;
      setActor({ ...actor(), ...profile });
      dirty = get("accountDisplayName").value !== name;
      status(t("updated"));
    } catch (error) { if (version === epoch) status(error.message, true); }
    finally { if (version === epoch) { pending = false; render(); } }
  });
  function clear() {
    ++epoch; request?.abort(); request = null; profile = null; dirty = pending = false;
    get("accountUsername").textContent = ""; get("accountRole").textContent = "";
    get("accountDisplayName").value = ""; status(); render();
  }
  document.addEventListener("sessionended", () => { owner = ""; clear(); });
  document.addEventListener("sessionchange", () => { if (owner !== accountId()) { owner = accountId(); clear(); } });
  document.addEventListener("localechange", render);
  return { load, close() { ++epoch; request?.abort(); request = null; pending = false; status(); render(); } };
}
