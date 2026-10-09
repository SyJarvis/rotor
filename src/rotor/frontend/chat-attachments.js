// Keep attachment bodies outside localStorage so large files do not evict chats.
let database;

function openDatabase() {
  if (!database) {
    database = new Promise((resolve, reject) => {
      if (!globalThis.indexedDB) return reject(new Error('Attachment storage is unavailable'));
      const request = indexedDB.open('rotor.chat.attachments.v1', 1);
      request.onupgradeneeded = () => request.result.createObjectStore('attachments', { keyPath: 'id' });
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error('Attachment storage is blocked'));
    }).catch((error) => { database = null; throw error; });
  }
  return database;
}

async function transaction(mode, operation) {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('attachments', mode);
    let result;
    const request = operation(tx.objectStore('attachments'));
    if (request) request.onsuccess = () => { result = request.result; };
    tx.oncomplete = () => resolve(result);
    tx.onerror = () => reject(tx.error || request?.error);
    tx.onabort = () => reject(tx.error || new Error('Attachment storage was interrupted'));
  });
}

export function saveAttachment(attachment) {
  return transaction('readwrite', (store) => store.put(attachment));
}

export function getAttachment(id) {
  return transaction('readonly', (store) => store.get(id));
}

export function deleteAttachments(ids) {
  if (!ids.length) return Promise.resolve();
  return transaction('readwrite', (store) => { for (const id of ids) store.delete(id); });
}
