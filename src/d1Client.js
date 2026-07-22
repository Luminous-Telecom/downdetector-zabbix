/**
 * d1Client.js
 * Cloudflare D1 REST API wrapper.
 *
 * This app runs as a local Node.js process (not a Cloudflare Worker),
 * so we access D1 via the REST API instead of a Worker binding.
 *
 * Required env vars:
 *   CLOUDFLARE_ACCOUNT_ID   — Cloudflare account ID (dashboard right sidebar)
 *   CLOUDFLARE_API_TOKEN    — API token with D1:Edit permission
 *   D1_DATABASE_ID          — Database UUID (defaults to downdetector-alarm UUID)
 *
 * Tables:
 *   summaries      — full history of every cron scrape (one row per run)
 *
 *   alerts         — live state, one row per service (created on first detection,
 *                    never deleted). Active incidents = status IN ('WARNING','DOWN').
 *                    round increments every scraping cycle the problem persists.
 *
 *   alerts_history — append-only incident log, one row per incident lifecycle.
 *                    Inserted on first detection (end_at NULL), updated each cycle
 *                    (round++, status), closed on resolution (end_at filled).
 *
 * Alert statuses:
 *   DOWN       — active, critical outage
 *   WARNING    — active, degraded service
 *   CLEAR      — resolved, service recovered normally
 *   NOT_FOUND  — resolved, service disappeared from scrape while alert was open
 */

const ACCOUNT_ID = (process.env.CLOUDFLARE_ACCOUNT_ID || '').trim();
const API_TOKEN = (process.env.CLOUDFLARE_API_TOKEN || '').trim();
const DB_ID = process.env.D1_DATABASE_ID || 'a4696e2f-3e65-40b1-a168-5fd957e593ad';

/** D1 é opcional — sem credenciais a API continua só com cache/HTTP. */
function isD1Configured() {
  return Boolean(ACCOUNT_ID && API_TOKEN);
}

function getD1Url() {
  return `https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/d1/database/${DB_ID}/query`;
}

/**
 * Execute a SQL statement against D1.
 * @param {string} sql
 * @param {Array}  params  — positional ? parameters
 * @returns {Promise<Array>} rows from result[0].results
 */
async function queryD1(sql, params = []) {
  if (!isD1Configured()) {
    throw new Error('[D1] CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN not set');
  }

  const res = await fetch(getD1Url(), {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${API_TOKEN}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ sql, params }),
  });

  const json = await res.json();

  if (!json.success) {
    const errMsg = JSON.stringify(json.errors);
    // Provide actionable guidance based on the HTTP status code
    if (res.status === 401 || res.status === 403) {
      console.error(
        '[D1] ❌ Authentication failed — check your API token.\n' +
        '  Guidance:\n' +
        '  1. CLOUDFLARE_API_TOKEN in .env must have "D1:Edit" permission.\n' +
        '     → Dashboard → My Profile → API Tokens → Edit token → check D1 permissions\n' +
        '  2. Token may have expired — regenerate and update .env\n' +
        '  3. CLOUDFLARE_ACCOUNT_ID must match the account that owns the D1 database.'
      );
    } else if (res.status === 404) {
      console.error(
        '[D1] ❌ Database not found — check database ID.\n' +
        '  Guidance:\n' +
        '  1. D1_DATABASE_ID in .env must match the UUID in wrangler.toml.\n' +
        '  2. Verify the database exists: Dashboard → Workers & Pages → D1\n' +
        '  3. Current DB ID: ' + DB_ID
      );
    } else {
      console.error('[D1] ❌ Query failed (HTTP ' + res.status + '):', errMsg);
    }
    throw new Error(`[D1] Query failed: ${errMsg}`);
  }

  return json.result?.[0]?.results ?? [];
}

// ---------------------------------------------------------------------------
// Schema init
// ---------------------------------------------------------------------------

/**
 * Drops and recreates all tables on startup.
 * Called once on server startup.
 */
async function initD1() {
  if (!isD1Configured()) {
    console.log('[D1] Desabilitado (sem CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN) — OK');
    return;
  }

  // summaries — unchanged
  await queryD1(`
    CREATE TABLE IF NOT EXISTS summaries (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      fetched_at TEXT NOT NULL,
      data       TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
  `);

  // Drop old single-table alerts if it exists
  // await queryD1(`DROP TABLE IF EXISTS alerts`);
  // await queryD1(`DROP TABLE IF EXISTS alerts_history`);

  // alerts — live state, one row per service (starts empty)
  await queryD1(`
    CREATE TABLE IF NOT EXISTS alerts (
      slug       TEXT PRIMARY KEY,
      service    TEXT NOT NULL,
      status     TEXT NOT NULL,
      round      INTEGER NOT NULL DEFAULT 1,
      start_time TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
  `);

  // alerts_history — append-only incident log
  await queryD1(`
    CREATE TABLE IF NOT EXISTS alerts_history (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      slug       TEXT NOT NULL,
      service    TEXT NOT NULL,
      status     TEXT NOT NULL,
      round      INTEGER NOT NULL DEFAULT 1,
      start_at   TEXT NOT NULL,
      end_at     TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
  `);

  console.log('[D1] Tables ready (summaries + alerts + alerts_history)');
}

// ---------------------------------------------------------------------------
// summaries table
// ---------------------------------------------------------------------------

/**
 * Returns the most recent summary data object, or null if no rows yet.
 * @returns {Promise<Object|null>}
 */
async function getLastSummary() {
  if (!isD1Configured()) return null;
  const rows = await queryD1(
    'SELECT data FROM summaries ORDER BY id DESC LIMIT 1'
  );
  if (!rows.length) return null;
  try {
    return JSON.parse(rows[0].data);
  } catch {
    return null;
  }
}

/**
 * Inserts a new row into summaries (history — never overwrites).
 * @param {Object} summaryData — the full homepage summary JSON
 */
async function insertSummary(summaryData) {
  if (!isD1Configured()) return;
  await queryD1(
    'INSERT INTO summaries (fetched_at, data) VALUES (?, ?)',
    [summaryData.fetchedAt, JSON.stringify(summaryData)]
  );
}

// ---------------------------------------------------------------------------
// alerts + alerts_history — active incident queries
// ---------------------------------------------------------------------------

/**
 * Fetches all active (open) alerts and returns them as a Map keyed by slug.
 * Active alerts are rows where status IN ('WARNING', 'DOWN').
 *
 * @returns {Promise<Map<string, {slug, name, status, round, startTime}>>}
 */
async function getActiveAlerts() {
  if (!isD1Configured()) return new Map();
  const rows = await queryD1(
    `SELECT slug, service, status, round, start_time
     FROM alerts
     WHERE status IN ('WARNING', 'DOWN')`
  );
  const map = new Map();
  for (const row of rows) {
    map.set(row.slug, {
      slug:      row.slug,
      name:      row.service,
      status:    row.status,
      round:     row.round,
      startTime: row.start_time,
    });
  }
  return map;
}

// ---------------------------------------------------------------------------
// alerts + alerts_history — write operations
// ---------------------------------------------------------------------------

/**
 * Creates (or resets) an alert row when a NEW incident is detected.
 * - If the service has no row yet → INSERT into alerts + INSERT into alerts_history
 * - If the service has an existing CLEAR/NOT_FOUND row → reset it + INSERT new history row
 * Round always starts at 1.
 *
 * @param {string} slug      — service slug
 * @param {string} service   — human-readable service name
 * @param {string} status    — 'DOWN' or 'WARNING'
 * @param {string} startTime — ISO 8601 string
 */
async function upsertNewAlert(slug, service, status, startTime) {
  if (!isD1Configured()) return;
  // Upsert alerts row (insert if new, replace/reset if existing CLEAR row)
  await queryD1(
    `INSERT INTO alerts (slug, service, status, round, start_time, updated_at)
     VALUES (?, ?, ?, 1, ?, ?)
     ON CONFLICT(slug) DO UPDATE SET
       service    = excluded.service,
       status     = excluded.status,
       round      = 1,
       start_time = excluded.start_time,
       updated_at = excluded.updated_at`,
    [slug, service, status, startTime, startTime]
  );

  // Insert a fresh alerts_history row for this incident lifecycle
  await queryD1(
    `INSERT INTO alerts_history (slug, service, status, round, start_at)
     VALUES (?, ?, ?, 1, ?)`,
    [slug, service, status, startTime]
  );
}

/**
 * Increments round and updates status on an ONGOING incident.
 * Updates both alerts (live state) and alerts_history (open row).
 *
 * @param {string} slug      — service slug
 * @param {string} status    — current status ('DOWN' or 'WARNING')
 * @param {string} updatedAt — ISO 8601 string (now)
 */
async function incrementAlertRound(slug, status, updatedAt) {
  if (!isD1Configured()) return;
  // Update alerts live state
  await queryD1(
    `UPDATE alerts
     SET round      = round + 1,
         status     = ?,
         updated_at = ?
     WHERE slug = ?`,
    [status, updatedAt, slug]
  );

  // Update the open alerts_history row (most recent with end_at NULL)
  await queryD1(
    `UPDATE alerts_history
     SET round  = round + 1,
         status = ?
     WHERE id = (
       SELECT id FROM alerts_history
       WHERE slug = ? AND end_at IS NULL
       ORDER BY id DESC
       LIMIT 1
     )`,
    [status, slug]
  );
}

/**
 * Closes an incident: fills end_at in alerts_history, copies the final round,
 * and updates status in alerts to CLEAR or NOT_FOUND.
 *
 * @param {string} slug    — service slug
 * @param {string} endTime — ISO 8601 string
 * @param {string} [status='CLEAR'] — closing status: 'CLEAR' or 'NOT_FOUND'
 */
async function resolveAlert(slug, endTime, status = 'CLEAR') {
  if (!isD1Configured()) return;
  // Read the current round from the live alerts row
  const rows = await queryD1(
    'SELECT round FROM alerts WHERE slug = ?',
    [slug]
  );
  const finalRound = rows[0]?.round ?? 1;

  // Close the open alerts_history row
  await queryD1(
    `UPDATE alerts_history
     SET end_at = ?,
         status = ?,
         round  = ?
     WHERE id = (
       SELECT id FROM alerts_history
       WHERE slug = ? AND end_at IS NULL
       ORDER BY id DESC
       LIMIT 1
     )`,
    [endTime, status, finalRound, slug]
  );

  // Update the live alerts row status
  await queryD1(
    `UPDATE alerts
     SET status     = ?,
         updated_at = ?
     WHERE slug = ?`,
    [status, endTime, slug]
  );
}

// ---------------------------------------------------------------------------
// alerts_history — history queries (for /api/alerts endpoint)
// ---------------------------------------------------------------------------

/**
 * Queries the alerts_history table with optional filters.
 *
 * @param {Object} opts
 * @param {string} [opts.service]  — filter by service name (exact match)
 * @param {string} [opts.status]   — filter by status ('DOWN', 'WARNING', 'CLEAR', 'NOT_FOUND')
 * @param {string} [opts.from]     — ISO date lower bound on start_at (inclusive)
 * @param {string} [opts.to]       — ISO date upper bound on start_at (inclusive)
 * @param {number} [opts.limit=50] — max rows to return
 * @returns {Promise<Array>}
 */
async function getAlerts({ service, status, from, to, limit = 50 } = {}) {
  if (!isD1Configured()) return [];
  const conditions = [];
  const params     = [];

  if (service) {
    conditions.push('service = ?');
    params.push(service);
  }
  if (status) {
    conditions.push('status = ?');
    params.push(status);
  }
  if (from) {
    conditions.push('start_at >= ?');
    params.push(from);
  }
  if (to) {
    conditions.push('start_at <= ?');
    params.push(to);
  }

  const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
  params.push(Math.min(Number(limit) || 50, 500)); // cap at 500

  return queryD1(
    `SELECT id, slug, service, status, round, start_at, end_at, created_at
     FROM alerts_history
     ${where}
     ORDER BY id DESC
     LIMIT ?`,
    params
  );
}

module.exports = {
  isD1Configured,
  initD1,
  getLastSummary,
  insertSummary,
  getActiveAlerts,
  upsertNewAlert,
  incrementAlertRound,
  resolveAlert,
  getAlerts,
};
