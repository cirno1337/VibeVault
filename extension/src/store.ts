/** Tiny key-value stores: IndexedDB in the extension, in-memory in tests. */

export interface KV<T> {
  get(key: string): Promise<T | undefined>;
  put(key: string, value: T): Promise<void>;
  delete(key: string): Promise<void>;
  entries(): Promise<[string, T][]>; // sorted by key
}

export class MemoryKV<T> implements KV<T> {
  readonly map = new Map<string, T>();
  async get(key: string) {
    return this.map.get(key);
  }
  async put(key: string, value: T) {
    this.map.set(key, structuredClone(value));
  }
  async delete(key: string) {
    this.map.delete(key);
  }
  async entries(): Promise<[string, T][]> {
    return [...this.map.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  }
}

const DB_NAME = "vibevault";
export const STORES = ["batches", "fingerprints"] as const;
let dbPromise: Promise<IDBDatabase> | undefined;

function openDb(): Promise<IDBDatabase> {
  dbPromise ??= new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      for (const s of STORES) if (!req.result.objectStoreNames.contains(s)) req.result.createObjectStore(s);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => {
      dbPromise = undefined;
      reject(req.error);
    };
  });
  return dbPromise;
}

function done<R>(req: IDBRequest<R>): Promise<R> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

export class IdbKV<T> implements KV<T> {
  constructor(private readonly store: (typeof STORES)[number]) {}

  private async tx(mode: IDBTransactionMode): Promise<IDBObjectStore> {
    return (await openDb()).transaction(this.store, mode).objectStore(this.store);
  }
  async get(key: string) {
    return done((await this.tx("readonly")).get(key)) as Promise<T | undefined>;
  }
  async put(key: string, value: T) {
    await done((await this.tx("readwrite")).put(value, key));
  }
  async delete(key: string) {
    await done((await this.tx("readwrite")).delete(key));
  }
  async entries(): Promise<[string, T][]> {
    const s = await this.tx("readonly");
    const [keys, values] = await Promise.all([done(s.getAllKeys()), done(s.getAll())]);
    return keys.map((k, i) => [String(k), values[i] as T]);
  }
}
