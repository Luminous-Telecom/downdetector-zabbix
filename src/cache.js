/**
 * cache.js
 * Simple in-memory TTL cache.
 *
 * Keys:
 *   "homepage"          → homepage summary
 *   "service:<slug>"    → service detail
 *
 * TTL controlled by CACHE_TTL_MS env var (default: 3 minutes).
 */

const DEFAULT_TTL_MS = parseInt(process.env.CACHE_TTL_MS || '', 10) || 3 * 60 * 1000;

const _store = new Map(); // key → { value, expiresAt }

function get(key) {
  const entry = _store.get(key);
  if (!entry) return null;
  if (Date.now() > entry.expiresAt) {
    _store.delete(key);
    return null;
  }
  return entry.value;
}

function set(key, value, ttlMs = DEFAULT_TTL_MS) {
  // ttlMs <= 0 → do not cache
  if (!ttlMs || ttlMs <= 0) return;
  _store.set(key, { value, expiresAt: Date.now() + ttlMs });
}

function invalidate(key) {
  _store.delete(key);
}

function clear() {
  _store.clear();
}

/**
 * Returns the cached value if fresh, otherwise fetches via fetchFn(), caches it, and returns it.
 * If fetchFn() throws and there IS a stale entry, returns the stale entry marked `stale: true`.
 * Pass forceRefresh=true or ttlMs<=0 to always fetch.
 */
async function getOrFetch(key, fetchFn, ttlMs = DEFAULT_TTL_MS, forceRefresh = false) {
  if (!forceRefresh && ttlMs > 0) {
    const fresh = get(key);
    if (fresh) {
      console.log(`[Cache] HIT for key "${key}"`);
      return fresh;
    }
  } else if (forceRefresh) {
    invalidate(key);
    console.log(`[Cache] REFRESH for key "${key}" — fetching...`);
  }

  if (!forceRefresh) {
    console.log(`[Cache] MISS for key "${key}" — fetching...`);
  }

  try {
    const value = await fetchFn();
    set(key, value, ttlMs);
    return value;
  } catch (err) {
    const staleEntry = _store.get(key);
    if (staleEntry) {
      console.warn(`[Cache] Fetch failed; returning stale result for "${key}": ${err.message}`);
      return { ...staleEntry.value, stale: true };
    }
    throw err;
  }
}

module.exports = { get, set, invalidate, clear, getOrFetch };
