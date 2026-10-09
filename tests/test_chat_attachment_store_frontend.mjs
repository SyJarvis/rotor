import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../src/rotor/frontend/chat-attachments.js', import.meta.url), 'utf8');
async function storeEnvironment() {
  const values = new Map(), transactions = [];
  let opens = 0, failOpen = false;
  const database = {
    createObjectStore(name, options) { assert.equal(name, 'attachments'); assert.equal(options.keyPath, 'id'); },
    transaction(name, mode) {
      assert.equal(name, 'attachments');
      const tx = { mode, objectStore() { return {
        put(value) { const request = {}; tx.commit = () => values.set(value.id, structuredClone(value)); return request; },
        get(id) { const request = {}; tx.request = request; tx.commit = () => { request.result = values.get(id); request.onsuccess?.(); }; return request; },
        delete(id) { const previous = tx.commit; tx.commit = () => { previous?.(); values.delete(id); }; },
      }; } };
      transactions.push(tx); return tx;
    },
  };
  const indexedDB = { open(name, version) {
    opens++; assert.equal(name, 'rotor.chat.attachments.v1'); assert.equal(version, 1);
    const request = { result: database };
    queueMicrotask(() => {
      if (failOpen) { failOpen = false; request.error = new Error('denied'); request.onerror(); }
      else { if (opens === 1) request.onupgradeneeded(); request.onsuccess(); }
    });
    return request;
  } };
  const context = vm.createContext({ indexedDB });
  const module = new vm.SourceTextModule(source, { context }); await module.link(() => {}); await module.evaluate();
  return { store: module.namespace, transactions, values, opens: () => opens, failNextOpen() { failOpen = true; },
    async commit() { await new Promise((resolve) => setImmediate(resolve)); const tx = transactions.at(-1); tx.commit?.(); tx.oncomplete(); },
    async abort() { await new Promise((resolve) => setImmediate(resolve)); const tx = transactions.at(-1); tx.error = new Error('quota'); tx.onabort(); } };
}

test('attachment writes resolve only after commit; payloads survive reads and selective deletion', async () => {
  const env = await storeEnvironment();
  let completed = false;
  const saving = env.store.saveAttachment({ id: 'first', dataUrl: 'data:image/png;base64,a' }).then(() => { completed = true; });
  await new Promise((resolve) => setImmediate(resolve)); assert.equal(completed, false);
  await env.commit(); await saving;
  const saving2 = env.store.saveAttachment({ id: 'second', content: 'keep' }); await env.commit(); await saving2;
  const reading = env.store.getAttachment('first'); await env.commit(); assert.equal((await reading).dataUrl, 'data:image/png;base64,a');
  const removing = env.store.deleteAttachments(['first']); await env.commit(); await removing;
  assert.equal(env.values.has('first'), false); assert.equal(env.values.has('second'), true); assert.equal(env.opens(), 1);
});

test('aborted attachment transactions reject without reporting a successful save', async () => {
  const env = await storeEnvironment();
  const saving = env.store.saveAttachment({ id: 'a', content: 'lost' });
  const rejected = assert.rejects(saving, /quota/); await env.abort(); await rejected;
  assert.equal(env.values.size, 0);
});

test('opening storage can recover after a failed attempt', async () => {
  const env = await storeEnvironment(); env.failNextOpen();
  await assert.rejects(env.store.getAttachment('a'), /denied/);
  const reading = env.store.getAttachment('a'); await env.commit(); assert.equal(await reading, undefined);
  assert.equal(env.opens(), 2);
});
