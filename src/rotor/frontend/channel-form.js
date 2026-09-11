// Connection defaults mirror provider_defaults; the server supplies provider presets.
let presets = [];
const previousDefaults = new WeakMap();

export function setChannelPresets(values) { presets = values; }

export function channelDefaults(provider, protocol = "openai") {
  const preset = presets.find((item) => item.id === provider) || {};
  const anthropic = ["anthropic", "anthropic_messages"].includes(protocol.toLowerCase());
  return {
    ...preset,
    models_path: preset.models_path || "/models",
    request_path: anthropic ? "/messages"
      : ["responses", "openai_responses"].includes(protocol.toLowerCase()) ? "/responses" : "/chat/completions",
    auth_type: preset.auth_type || (anthropic ? "x-api-key" : "bearer"),
  };
}

export function rememberChannelDefaults(form) {
  previousDefaults.set(form, channelDefaults(form.elements.type.value, form.elements.protocol.value));
}

export function applyChannelDefaults(form, { providerChanged = false, reset = false } = {}) {
  const old = previousDefaults.get(form) || {};
  const preset = presets.find((item) => item.id === form.elements.type.value);
  if ((providerChanged || reset) && preset) form.elements.protocol.value = preset.protocol;
  const defaults = channelDefaults(form.elements.type.value, form.elements.protocol.value);
  for (const name of ["base_url", "models_path", "request_path", "auth_type"]) {
    const field = form.elements[name];
    if (defaults[name] !== undefined && (reset || !field.value.trim() || field.value === old[name])) {
      field.value = defaults[name];
    }
  }
  rememberChannelDefaults(form);
}

export function channelConnection(form, extra = {}) {
  const fields = form.elements;
  const defaults = channelDefaults(fields.type.value, fields.protocol.value);
  return {
    base_url: fields.base_url.value.trim(),
    type: fields.type.value,
    protocol: fields.protocol.value,
    extra: {
      ...extra,
      models_path: fields.models_path.value.trim() || defaults.models_path,
      request_path: fields.request_path.value.trim() || defaults.request_path,
      auth_type: fields.auth_type.value || defaults.auth_type,
    },
  };
}
