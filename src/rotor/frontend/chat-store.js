import { api } from "./api.js?v=3";

// The server owns message history. Only settings and the unsent draft are patched.
export class ChatStore {
  constructor() { this.records = new Map(); this.writers = new Map(); this.requests = new Set(); this.epoch = 0; }
  reset() { ++this.epoch; for (const controller of this.requests) controller.abort(); this.requests.clear(); this.records.clear(); this.writers.clear(); }
  async request(path, options = {}) {
    const epoch = this.epoch, controller = new AbortController(); this.requests.add(controller);
    try {
      const result = await api(`/api/chat/conversations${path}`, { ...options, signal: controller.signal });
      if (epoch !== this.epoch) throw new DOMException("Session changed", "AbortError");
      return result;
    } finally { this.requests.delete(controller); }
  }
  accept(conversation) { if ((this.records.get(conversation.id)?.revision ?? -1) <= conversation.revision) this.records.set(conversation.id, structuredClone(conversation)); return conversation; }
  async list() { return this.request(""); }
  async get(id) { return this.accept(await this.request(`/${encodeURIComponent(id)}`)); }
  async create(settings, draft = { text: "", attachments: [] }) { return this.accept(await this.request("", { method: "POST", body: JSON.stringify({ settings, draft }) })); }
  save(id, changes) {
    let writer = this.writers.get(id);
    if (!writer) { writer = { pending: null, running: null, conflict: false }; this.writers.set(id, writer); }
    writer.pending = { ...writer.pending, ...structuredClone(changes) };
    if (writer.conflict) return Promise.reject(Object.assign(new Error("Conversation changed on another device"), { status: 409 }));
    if (!writer.running) {
      const epoch = this.epoch;
      writer.running = (async () => {
        while (writer.pending && epoch === this.epoch) {
          const snapshot = writer.pending; writer.pending = null;
          try {
            this.accept(await this.request(`/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ expected_revision: this.records.get(id).revision, ...(Object.keys(snapshot).length === 1 && snapshot.draft ? { expected_draft_revision: this.records.get(id).draftRevision } : {}), ...snapshot }) }));
          } catch (error) {
            writer.pending = { ...snapshot, ...writer.pending };
            if (error.status === 409) { writer.conflict = true; try { await this.get(id); } catch {} }
            throw error;
          }
        }
        return this.records.get(id);
      })().finally(() => { writer.running = null; });
    }
    return writer.running;
  }
  async flush(id) { const writer = this.writers.get(id); if (writer?.conflict) throw Object.assign(new Error("Conversation changed on another device"), { status: 409 }); if (writer?.running) await writer.running; if (writer?.pending) await this.save(id, {}); return this.records.get(id); }
  async remove(id) { await this.flush(id); await this.request(`/${encodeURIComponent(id)}?expected_revision=${this.records.get(id).revision}`, { method: "DELETE" }); this.records.delete(id); this.writers.delete(id); }
  async upload(id, attachment) { return this.request(`/${encodeURIComponent(id)}/attachments`, { method: "POST", body: JSON.stringify(attachment) }); }
  async attachment(id, attachmentId) { return this.request(`/${encodeURIComponent(id)}/attachments/${encodeURIComponent(attachmentId)}`); }
  async removeAttachment(id, attachmentId) { return this.request(`/${encodeURIComponent(id)}/attachments/${encodeURIComponent(attachmentId)}`, { method: "DELETE" }); }
}
