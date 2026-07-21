/**
 * index.js — Downdetector Scraper Health-Check Worker
 *
 * Runs on a Cloudflare cron trigger every 20 minutes.
 * Checks the `summaries` table in D1: if the latest row's `created_at`
 * is older than STALE_THRESHOLD_MIN (50 min), the scraper is assumed DOWN
 * and an alert is sent to the Power Automate webhook.
 *
 * Secrets (set with `wrangler secret put`):
 *   HEALTH_CHECK_WEBHOOK_URL — Power Automate HTTP-trigger URL
 *
 * D1 binding (wrangler.toml):
 *   DB — bound to the downdetector-alarm database
 *
 * Alert behaviour:
 *   • Fires on EVERY cron tick (every 20 min) while the server is down
 *   • No recovery alert (by design)
 */

// How old the latest summary may be before we assume the server is down (minutes)
const STALE_THRESHOLD_MIN = 50;

// Bangkok timezone label for display
const TZ = 'Asia/Bangkok';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Format a UTC Date object as "YYYY-MM-DD HH:MM:SS (UTC+7)".
 * @param {Date} date
 * @returns {string}
 */
function toBKKDateTime(date) {
  const parts = date.toLocaleString('sv-SE', { timeZone: TZ });
  return `${parts} (UTC+7)`;
}

/**
 * Build the HTML alert body sent to Power Automate.
 *
 * @param {Date}   lastScrapeAt  — UTC Date of the latest summary row
 * @param {Date}   checkedAt     — UTC Date when the Worker ran
 * @param {number} staleMins     — How many minutes since the last scrape
 * @returns {string} HTML string
 */
function buildAlertHtml(lastScrapeAt, checkedAt, staleMins) {
  const lastStr    = toBKKDateTime(lastScrapeAt);
  const checkedStr = toBKKDateTime(checkedAt);

  return [
    '<b>Downdetector TH Alert</b>',
    checkedStr,
    '',
    'Status: <span style="color:#e81123">Alert Server not responding</span>',
    `Last successful scrape: ${lastStr}`,
    `Time since last scrape: ${staleMins} minutes`,
    `Alert threshold: ${STALE_THRESHOLD_MIN} minutes`,
    '',
    '<b>Recommended Action:</b>',
    '1. Check if server is running properly',
    "2. If not, please restart server by using <b>Ctrl+C</b> and type command <b>'npm run dev'</b> at terminal console",
  ].join('<br>') + '<br>';
}

/**
 * POST the alert to Power Automate.
 * @param {string} webhookUrl
 * @param {Date}   lastScrapeAt
 * @param {Date}   checkedAt
 * @param {number} staleMins
 */
async function sendAlert(webhookUrl, lastScrapeAt, checkedAt, staleMins) {
  const htmlMessage = buildAlertHtml(lastScrapeAt, checkedAt, staleMins);

  const payload = {
    type:         'scraper_health_alert',
    checkedAt:    checkedAt.toISOString(),
    lastScrapeAt: lastScrapeAt.toISOString(),
    staleMins,
    thresholdMins: STALE_THRESHOLD_MIN,
    htmlMessage,
  };

  const res = await fetch(webhookUrl, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(payload),
  });

  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`[HealthCheck] Webhook returned ${res.status}: ${body.slice(0, 300)}`);
  }

  console.log(`[HealthCheck] Alert sent — stale by ${staleMins} min, webhook status ${res.status}`);
}

// ---------------------------------------------------------------------------
// Worker entry point
// ---------------------------------------------------------------------------

export default {
  /**
   * Cron handler — fires every 20 minutes per wrangler.toml [triggers].
   *
   * @param {ScheduledEvent}  event
   * @param {{ DB: D1Database, HEALTH_CHECK_WEBHOOK_URL: string }} env
   * @param {ExecutionContext} ctx
   */
  async scheduled(event, env, ctx) {
    const checkedAt = new Date(event.scheduledTime);

    console.log(`[HealthCheck] Cron fired at ${checkedAt.toISOString()}`);

    // ── 1. Validate secrets / bindings ──────────────────────────────────────
    if (!env.HEALTH_CHECK_WEBHOOK_URL) {
      console.error(
        '[HealthCheck] HEALTH_CHECK_WEBHOOK_URL secret is not set.\n' +
        '  → Run: wrangler secret put HEALTH_CHECK_WEBHOOK_URL\n' +
        '  → Then paste your Power Automate HTTP-trigger URL.'
      );
      return;
    }

    if (!env.DB) {
      console.error(
        '[HealthCheck] D1 binding "DB" is missing.\n' +
        '  → Verify [[d1_databases]] in worker/wrangler.toml.\n' +
        '  → Make sure database_id matches your D1 database UUID.'
      );
      return;
    }

    // ── 2. Query the most recent summary row ────────────────────────────────
    let lastScrapeAt;

    try {
      const result = await env.DB
        .prepare('SELECT created_at FROM summaries ORDER BY id DESC LIMIT 1')
        .first();

      if (!result) {
        // No rows yet — the scraper has never run or D1 is empty.
        console.warn(
          '[HealthCheck] No rows in summaries table — scraper may never have run, ' +
          'or the D1 database is wrong. Skipping alert.'
        );
        return;
      }

      lastScrapeAt = new Date(result.created_at);

      if (isNaN(lastScrapeAt.getTime())) {
        console.error(
          `[HealthCheck] created_at value "${result.created_at}" is not a valid date. ` +
          'Check the summaries table schema.'
        );
        return;
      }
    } catch (err) {
      console.error(
        '[HealthCheck] D1 query failed:', err.message, '\n' +
        '  → Check that the DB binding is correct in wrangler.toml.\n' +
        '  → Verify the D1 database ID and that the summaries table exists.\n' +
        '  → Ensure your Cloudflare account has D1 access.'
      );
      return;
    }

    // ── 3. Compare timestamps ────────────────────────────────────────────────
    const staleMins = Math.round((checkedAt.getTime() - lastScrapeAt.getTime()) / 60_000);

    console.log(
      `[HealthCheck] Last scrape: ${lastScrapeAt.toISOString()} ` +
      `(${staleMins} min ago, threshold: ${STALE_THRESHOLD_MIN} min)`
    );

    if (staleMins <= STALE_THRESHOLD_MIN) {
      console.log('[HealthCheck] Scraper is healthy — no alert needed.');
      return;
    }

    // ── 4. Server presumed DOWN — send alert ─────────────────────────────────
    console.warn(
      `[HealthCheck] ⚠️  Scraper has not reported for ${staleMins} minutes — sending alert.`
    );

    try {
      await sendAlert(env.HEALTH_CHECK_WEBHOOK_URL, lastScrapeAt, checkedAt, staleMins);
    } catch (err) {
      console.error(
        '[HealthCheck] Failed to send alert:', err.message, '\n' +
        '  → Check HEALTH_CHECK_WEBHOOK_URL is a valid Power Automate URL.\n' +
        '  → Verify the PA flow "When an HTTP request is received" trigger is active.\n' +
        '  → Check Worker logs in the Cloudflare dashboard for details.'
      );
    }
  },

  /**
   * HTTP fetch handler — allows manual trigger via HTTP GET for testing.
   * Example: wrangler dev → curl "http://localhost:8787/"
   *
   * @param {Request} request
   * @param {{ DB: D1Database, HEALTH_CHECK_WEBHOOK_URL: string }} env
   */
  async fetch(request, env) {
    const url = new URL(request.url);

    // Manual trigger for local testing
    if (url.pathname === '/trigger' && request.method === 'GET') {
      try {
        const fakeEvent = { scheduledTime: Date.now() };
        await this.scheduled(fakeEvent, env, {});
        return new Response('Health check triggered — see logs.', { status: 200 });
      } catch (err) {
        return new Response(`Error: ${err.message}`, { status: 500 });
      }
    }

    return new Response(
      JSON.stringify({
        name:        'downdetector-health-check',
        description: 'Cloudflare Worker cron job — monitors the Downdetector scraper server.',
        schedule:    'Every 20 minutes',
        threshold:   `${STALE_THRESHOLD_MIN} minutes`,
        endpoints: [
          'GET /trigger — manually trigger the health check (for local testing)',
        ],
      }, null, 2),
      { status: 200, headers: { 'Content-Type': 'application/json' } }
    );
  },
};
