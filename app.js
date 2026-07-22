/**
 * app.js — Downdetector BR HTTP API
 *
 * Coleta apenas via FlareSolverr (sem Puppeteer/Chrome).
 * Endpoints /api/* exigem token (API_TOKEN no .env).
 *
 * Auth (qualquer um):
 *   Authorization: Bearer <API_TOKEN>
 *   X-API-Token: <API_TOKEN>
 *   ?token=<API_TOKEN>
 */

require('dotenv').config();

const http = require('http');
const url = require('url');
const crypto = require('crypto');

const { scrapeHomepage } = require('./src/homepage');
const { scrapeService } = require('./src/service');
const cache = require('./src/cache');
const { processServices } = require('./src/statusDiff');
const {
  initD1,
  insertSummary,
  getActiveAlerts,
  upsertNewAlert,
  incrementAlertRound,
  resolveAlert,
  getAlerts,
} = require('./src/d1Client');
const { sendTeamsNotification } = require('./src/notifier');
const { purgeOldScreenshots } = require('./src/r2Uploader');

const PORT = parseInt(process.env.PORT || '3333', 10);
const API_TOKEN = (process.env.API_TOKEN || '').trim();

const SUMMARY_INTERVAL_MS =
  parseInt(process.env.SUMMARY_INTERVAL_MS || '', 10) || 15 * 60 * 1000;
const SUMMARY_JITTER_MS = 60 * 1000;
const CACHE_TTL_MS =
  parseInt(process.env.CACHE_TTL_MS || '', 10) || SUMMARY_INTERVAL_MS;
const SERVICE_CACHE_TTL_MS =
  parseInt(process.env.SERVICE_CACHE_TTL_MS || '', 10) || 60 * 1000;

if (!API_TOKEN) {
  console.error(
    '[Auth] ❌ API_TOKEN não definido no .env — a API não vai iniciar.\n' +
      '  Gere um token: openssl rand -hex 32\n' +
      '  Depois: API_TOKEN=seu_token_aqui'
  );
  process.exit(1);
}

// --- Auth ---

function extractToken(req, query) {
  const auth = req.headers.authorization || '';
  if (auth.toLowerCase().startsWith('bearer ')) {
    return auth.slice(7).trim();
  }
  if (req.headers['x-api-token']) {
    return String(req.headers['x-api-token']).trim();
  }
  if (query && query.token) {
    return String(query.token).trim();
  }
  return '';
}

function tokensEqual(a, b) {
  const ba = Buffer.from(a);
  const bb = Buffer.from(b);
  if (ba.length !== bb.length) return false;
  return crypto.timingSafeEqual(ba, bb);
}

function requireAuth(req, res, query) {
  const provided = extractToken(req, query);
  if (!provided || !tokensEqual(provided, API_TOKEN)) {
    sendError(res, 401, 'Unauthorized', 'Informe API_TOKEN via Authorization: Bearer, X-API-Token ou ?token=');
    return false;
  }
  return true;
}

// --- Cron ---

let cronRunning = false;
let cronTimer = null;

async function runSummaryCron() {
  if (cronRunning) {
    console.log('[Cron] Previous run still in progress — skipping this tick.');
    return;
  }
  cronRunning = true;
  console.log(`[Cron] Homepage scrape started at ${new Date().toISOString()}`);

  try {
    const data = await scrapeHomepage();
    cache.set('homepage', data, CACHE_TTL_MS);
    console.log(
      `[Cron] Homepage scraped OK — ${data.totalServicesListed} services, fetchedAt ${data.fetchedAt}`
    );

    try {
      const incidents = await getActiveAlerts();
      const result = processServices(data, incidents);

      for (const inc of result.dbOps.upsert) {
        if (inc.isNew) {
          await upsertNewAlert(inc.slug, inc.name, inc.status, inc.startTime);
        } else {
          await incrementAlertRound(inc.slug, inc.status, new Date().toISOString());
        }
      }
      for (const r of result.dbOps.resolve) {
        await resolveAlert(r.slug, new Date().toISOString(), r.status);
      }

      await insertSummary(data);

      const screenshotUrls = new Map();
      purgeOldScreenshots(7).catch((purgeErr) => {
        console.warn('[Cron] R2 purge failed (non-fatal):', purgeErr.message);
      });

      await sendTeamsNotification(result, screenshotUrls);
      if (result.active.length || result.resolved.length) {
        console.log(
          `[Cron] Diff — active:${result.active.length} resolved:${result.resolved.length}`
        );
      }
    } catch (notifyErr) {
      console.error(
        '[Cron] Notify/D1 error (scrape result still cached):',
        notifyErr.message
      );
    }
  } catch (err) {
    console.error(
      '[Cron] ❌ Homepage scrape FAILED:',
      err.message,
      '\n  Verifique FLARESOLVERR_URL e se o container flaresolverr está no ar.'
    );
  } finally {
    cronRunning = false;
    scheduleNextCron();
  }
}

function scheduleNextCron() {
  const jitter = Math.floor((Math.random() * 2 - 1) * SUMMARY_JITTER_MS);
  const delay = SUMMARY_INTERVAL_MS + jitter;
  const nextAt = new Date(Date.now() + delay);
  console.log(
    `[Cron] Next homepage refresh in ${Math.round(delay / 1000)}s (at ${nextAt.toISOString()})`
  );
  cronTimer = setTimeout(runSummaryCron, delay);
}

// --- Response helpers ---

function sendJSON(res, statusCode, data) {
  const body = JSON.stringify(data, null, 2);
  res.writeHead(statusCode, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Authorization, X-API-Token, Content-Type',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

function sendError(res, statusCode, message, details) {
  sendJSON(res, statusCode, { error: message, details: details || null });
}

// --- Handlers ---

async function handleSummary(res, query = {}) {
  const forceRefresh = query.refresh === '1' || query.refresh === 'true';

  if (!forceRefresh) {
    const cached = cache.get('homepage');
    if (cached) {
      console.log(
        `[HTTP] /api/summary cache HIT — ${cached.totalServicesListed} services, fetchedAt ${cached.fetchedAt}`
      );
      return sendJSON(res, 200, cached);
    }
  } else {
    cache.invalidate('homepage');
    console.log('[HTTP] /api/summary ?refresh=1 — scraping...');
  }

  if (!forceRefresh) {
    console.log('[HTTP] /api/summary cache cold — scraping on demand...');
  }
  const data = await scrapeHomepage();
  cache.set('homepage', data, CACHE_TTL_MS);
  console.log(
    `[HTTP] /api/summary scraped OK — ${data.totalServicesListed} services, fetchedAt ${data.fetchedAt}`
  );
  sendJSON(res, 200, data);
}

async function handleService(res, slug, query = {}) {
  if (!slug || !/^[a-z0-9-]+$/i.test(slug)) {
    return sendError(res, 400, 'Invalid slug. Must be alphanumeric with hyphens.');
  }

  const forceRefresh = query.refresh === '1' || query.refresh === 'true';
  const cacheKey = `service:${slug.toLowerCase()}`;
  const data = await cache.getOrFetch(
    cacheKey,
    () => scrapeService(slug.toLowerCase()),
    SERVICE_CACHE_TTL_MS,
    forceRefresh
  );
  console.log(
    `[HTTP] /api/service/${slug.toLowerCase()} OK — status=${data.status}, reports=${data.reports}, fetchedAt ${data.fetchedAt}`
  );
  sendJSON(res, 200, data);
}

function handleServiceList(res) {
  const services = require('./src/config/services.json');
  sendJSON(res, 200, { services });
}

async function handleAlerts(res, query) {
  const { service, status, from, to, limit } = query;
  const rows = await getAlerts({ service, status, from, to, limit });
  sendJSON(res, 200, { total: rows.length, alerts: rows });
}

function handleIndex(res) {
  const nextCronMs = cronTimer
    ? cronTimer._idleStart + cronTimer._idleTimeout - Date.now()
    : null;
  sendJSON(res, 200, {
    name: 'Downdetector BR Scraper API',
    version: '2.0.0',
    collector: 'flaresolverr',
    auth: 'required on /api/* (Bearer / X-API-Token / ?token=)',
    cron: {
      intervalMs: SUMMARY_INTERVAL_MS,
      jitterMs: SUMMARY_JITTER_MS,
      running: cronRunning,
      nextRunInMs: nextCronMs != null ? Math.max(0, Math.round(nextCronMs)) : null,
    },
    endpoints: [
      'GET /              → help + cron status (público)',
      'GET /api/services  → lista pré-definida (auth)',
      'GET /api/summary   → resumo homepage (auth; ?refresh=1 força)',
      'GET /api/service/:slug → detalhe (auth; sem cache; ?refresh=1 força)',
      'GET /api/alerts    → histórico de alertas (auth)',
    ],
    examples: [
      `curl -H "Authorization: Bearer $API_TOKEN" http://localhost:${PORT}/api/service/caixa`,
      `curl -H "Authorization: Bearer $API_TOKEN" "http://localhost:${PORT}/api/summary?refresh=1"`,
    ],
  });
}

// --- HTTP server ---

const server = http.createServer(async (req, res) => {
  const parsed = url.parse(req.url || '/', true);
  const pathname = parsed.pathname || '/';

  console.log(`[HTTP] ${req.method} ${pathname}`);

  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': 'Authorization, X-API-Token, Content-Type',
    });
    return res.end();
  }

  if (req.method !== 'GET') {
    return sendError(res, 405, 'Method not allowed. Use GET.');
  }

  try {
    if (pathname === '/' || pathname === '/api') {
      return handleIndex(res);
    }

    // Tudo sob /api/* exige token
    if (pathname.startsWith('/api/')) {
      if (!requireAuth(req, res, parsed.query)) return;
    }

    if (pathname === '/api/services') {
      return handleServiceList(res);
    }

    if (pathname === '/api/summary') {
      return await handleSummary(res, parsed.query);
    }

    const serviceMatch = pathname.match(/^\/api\/service\/([^/]+)$/);
    if (serviceMatch) {
      return await handleService(res, serviceMatch[1], parsed.query);
    }

    if (pathname === '/api/alerts') {
      return await handleAlerts(res, parsed.query);
    }

    return sendError(res, 404, `Unknown endpoint: ${pathname}`);
  } catch (err) {
    console.error('[HTTP Error]', err.message);
    sendError(res, 500, 'Internal scraper error', err.message);
  }
});

server.listen(PORT, () => {
  console.log(`\n🚀 Downdetector BR Scraper API (FlareSolverr only)`);
  console.log(`   Listening at:  http://localhost:${PORT}`);
  console.log(`   Auth:          API_TOKEN required on /api/*`);
  console.log(`   Summary:       http://localhost:${PORT}/api/summary`);
  console.log(`   Service:       http://localhost:${PORT}/api/service/<slug>`);
  console.log(
    `\n📅 Background cron: homepage refresh every ~${Math.round(SUMMARY_INTERVAL_MS / 60000)} min`
  );
  console.log(`   Running first scrape now...`);

  initD1().catch((err) => {
    console.error('[D1] ❌ Table init FAILED:', err.message);
  });

  runSummaryCron();
});

process.on('unhandledRejection', (reason) => {
  console.error('[Process] Unhandled rejection:', reason);
});

process.on('uncaughtException', (err) => {
  console.error('[Process] Uncaught exception — exiting:', err.message);
  process.exit(1);
});

process.on('SIGINT', () => {
  console.log('\n[Server] Shutting down...');
  if (cronTimer) clearTimeout(cronTimer);
  server.close(() => {
    console.log('[Server] Stopped.');
    process.exit(0);
  });
});
