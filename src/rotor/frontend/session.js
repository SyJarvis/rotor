export const actor = () => window.ROTOR_ACTOR || null;
export const isAdmin = () => actor()?.role === "admin";
export const accountId = () => String(actor()?.user_id ?? actor()?.id ?? "");
export function setActor(value) {
  window.ROTOR_ACTOR = value;
  document.dispatchEvent(new CustomEvent("sessionchange", { detail: value }));
}
export function endSession() {
  window.ROTOR_ACTOR = null;
  document.dispatchEvent(new CustomEvent("sessionended"));
}
export function requestLogout() { window.dispatchEvent(new CustomEvent("rotorlogout")); }
