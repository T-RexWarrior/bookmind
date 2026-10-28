/** All legacy browser identifiers are confined to this migration boundary. */
export const LEGACY_STORAGE_PREFIX = 'deeptutor'

/** Run before theme selection and provider hydration. Never overwrite new keys. */
export const STORAGE_MIGRATION_SCRIPT = `
(function () {
  ['localStorage', 'sessionStorage'].forEach(function (kind) {
    try {
      var storage = window[kind];
      if (storage.getItem('bookmind.brandMigration.v1') === 'done') return;
      Object.keys(storage).forEach(function (key) {
        if (!/^deeptutor(?=[.:-])/.test(key)) return;
        var destination = key.replace(/^deeptutor/, 'bookmind');
        if (storage.getItem(destination) === null) {
          storage.setItem(destination, storage.getItem(key));
        }
      });
      storage.setItem('bookmind.brandMigration.v1', 'done');
    } catch (_) { /* unavailable storage must not prevent startup */ }
  });
})();`

const MIGRATION_STORE = '__bookmind_migrations'
type RecordMapper = (store: string, value: unknown, key: IDBValidKey) => unknown

/** Open an existing database without leaving an empty legacy database behind. */
function legacyDatabase(name: string): Promise<IDBDatabase | null> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(name.replace(/^bookmind/, LEGACY_STORAGE_PREFIX))
    let absent = false
    request.onupgradeneeded = () => { absent = true; request.transaction!.abort() }
    request.onsuccess = () => {
      request.result.onversionchange = () => request.result.close()
      resolve(request.result)
    }
    request.onerror = () => absent ? resolve(null) : reject(request.error)
  })
}

async function copyLegacyRecords(db: IDBDatabase, mapRecord?: RecordMapper): Promise<void> {
  const done = await new Promise<boolean>((resolve, reject) => {
    const request = db.transaction(MIGRATION_STORE).objectStore(MIGRATION_STORE).get('legacy')
    request.onsuccess = () => resolve(request.result === true)
    request.onerror = () => reject(request.error)
  })
  if (done) return
  const legacy = await legacyDatabase(db.name)
  const records: { store: string; key: IDBValidKey; value: unknown }[] = []
  if (legacy) {
    try {
      const stores = Array.from(legacy.objectStoreNames).filter(name => db.objectStoreNames.contains(name) && name !== MIGRATION_STORE)
      if (stores.length) await new Promise<void>((resolve, reject) => {
        const tx = legacy.transaction(stores)
        for (const store of stores) {
          const request = tx.objectStore(store).openCursor()
          request.onsuccess = () => {
            const cursor = request.result
            if (!cursor) return
            records.push({ store, key: cursor.primaryKey, value: mapRecord ? mapRecord(store, cursor.value, cursor.primaryKey) : cursor.value })
            cursor.continue()
          }
        }
        tx.oncomplete = () => resolve()
        tx.onerror = () => reject(tx.error)
        tx.onabort = () => reject(tx.error)
      })
    } finally { legacy.close() }
  }
  // Copy and mark atomically. Old data stays untouched; retries do not restore
  // records that the user deleted from the new database after migration.
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(Array.from(db.objectStoreNames), 'readwrite')
    for (const row of records) {
      const store = tx.objectStore(row.store)
      const key = typeof store.keyPath === 'string'
        ? (row.value as Record<string, IDBValidKey>)[store.keyPath] : row.key
      const prior = store.get(key)
      prior.onsuccess = () => {
        if (prior.result === undefined) {
          if (store.keyPath === null) store.put(row.value, row.key)
          else store.put(row.value)
        }
      }
    }
    tx.objectStore(MIGRATION_STORE).put(true, 'legacy')
    tx.oncomplete = () => resolve()
    tx.onerror = () => reject(tx.error)
    tx.onabort = () => reject(tx.error)
  })
}

export async function openBookMindDatabase(
  name: string,
  version: number,
  upgrade: (db: IDBDatabase, tx: IDBTransaction, oldVersion: number) => void,
  mapLegacyRecord?: RecordMapper,
): Promise<IDBDatabase> {
  const db = await new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(name, version)
    request.onupgradeneeded = event => {
      upgrade(request.result, request.transaction!, event.oldVersion)
      if (!request.result.objectStoreNames.contains(MIGRATION_STORE)) request.result.createObjectStore(MIGRATION_STORE)
    }
    request.onsuccess = () => {
      request.result.onversionchange = () => request.result.close()
      resolve(request.result)
    }
    request.onerror = () => reject(request.error)
  })
  try { await copyLegacyRecords(db, mapLegacyRecord); return db }
  catch (error) { db.close(); throw error }
}
