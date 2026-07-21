/**
 * statusDiff.js
 *
 * Processes the current scrape result against the active alerts map
 * to determine which services are active, newly started, or just resolved.
 *
 * Uses the alerts table (status IN ('WARNING','DOWN') = active) instead of
 * a separate incidents table.
 *
 * Status mapping → display label:
 *   DOWN       → 🔴 Critical
 *   WARNING    → 🟡 Warning
 *   Clear      → 🟢 Clear   (resolved normally)
 *   NOT_FOUND  → 🟣 Not Found (service disappeared from scrape while alert was open)
 */

// ---------------------------------------------------------------------------
// Time helpers (America/Sao_Paulo = UTC-3)
// ---------------------------------------------------------------------------

/**
 * Returns "HH:MM" in Brazil timezone.
 */
function toBKKTime(date) {
  return date.toLocaleTimeString('pt-BR', {
    timeZone: 'America/Sao_Paulo',
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
  });
}

/**
 * Returns "YYYY-MM-DD HH:MM:SS (UTC-3)" in Brazil timezone.
 */
function toBKKDateTime(date = new Date()) {
  const parts = date.toLocaleString('sv-SE', { timeZone: 'America/Sao_Paulo' });
  return `${parts} (UTC-3)`;
}

// ---------------------------------------------------------------------------
// Core processing
// ---------------------------------------------------------------------------

/**
 * Process the current scrape result against the active alerts map.
 *
 * @param {Object} currentSummary   — the current homepage scrape result
 * @param {Map}    activeAlerts     — Map<slug, {slug, name, status, round, startTime (ISO string)}>
 *                                    (rows from alerts WHERE status IN ('WARNING','DOWN'))
 *
 * @returns {{
 *   timestamp: string,
 *   active: Array,    — currently non-OK services (DOWN/WARNING)
 *   resolved: Array,  — services that just recovered or were not found this run
 *   dbOps: {
 *     upsert:  Array<{slug, name, status, startTime, isNew}>,
 *     resolve: Array<{slug, status}>   — "CLEAR" or "NOT_FOUND"
 *   },
 *   totalServices: number
 * }}
 */
function processServices(currentSummary, activeAlerts) {
  const now = new Date();
  const timestamp = toBKKDateTime(now);

  const active   = [];
  const resolved = [];
  const dbOps    = { upsert: [], resolve: [] };

  // Track every slug seen in this scrape (used for NOT_FOUND detection below)
  const seenSlugs = new Set();

  for (const service of (currentSummary.services || [])) {
    const { slug, name, status } = service;
    seenSlugs.add(slug);
    const existing = activeAlerts.get(slug);

    if (status !== 'OK') {
      if (!existing) {
        // ── NEW incident ──────────────────────────────────────────────────
        const startTimeISO = now.toISOString();
        dbOps.upsert.push({ slug, name, status, startTime: startTimeISO, isNew: true });
        active.push({
          slug,
          name,
          status,
          companyId:    service.companyId ?? null,
          round:        1,
          startTimeStr: toBKKTime(now),
          endTimeStr:   null,
          durationMin:  null,
          isNew:        true,
        });

      } else {
        // ── ONGOING incident (status may have escalated/de-escalated) ─────
        const startDate  = new Date(existing.startTime);
        const nextRound  = existing.round + 1;
        dbOps.upsert.push({ slug, name, status, startTime: existing.startTime, isNew: false });
        active.push({
          slug,
          name,
          status,
          companyId:    service.companyId ?? null,
          round:        nextRound,
          startTimeStr: toBKKTime(startDate),
          endTimeStr:   null,
          durationMin:  null,
          isNew:        false,
        });
      }

    } else if (existing) {
      // ── RESOLVED this run (service is back to OK) ─────────────────────
      const startDate   = new Date(existing.startTime);
      const durationMin = Math.max(1, Math.round((now - startDate) / 60000));
      resolved.push({
        slug,
        name,
        status:       'CLEAR',
        companyId:    service.companyId ?? null,
        round:        existing.round, // final round matches the last detection
        startTimeStr: toBKKTime(startDate),
        endTimeStr:   toBKKTime(now),
        durationMin,
      });
      dbOps.resolve.push({ slug, status: 'CLEAR' });
    }
    // status OK and no existing alert → nothing to do
  }

  // ── NOT_FOUND: active alerts whose slug never appeared in this scrape ──
  for (const [slug, existing] of activeAlerts) {
    if (!seenSlugs.has(slug)) {
      const startDate   = new Date(existing.startTime);
      const durationMin = Math.max(1, Math.round((now - startDate) / 60000));
      resolved.push({
        slug,
        name:         existing.name,
        status:       'NOT_FOUND',
        round:        existing.round, // final round matches the last detection
        startTimeStr: toBKKTime(startDate),
        endTimeStr:   toBKKTime(now),
        durationMin,
      });
      dbOps.resolve.push({ slug, status: 'NOT_FOUND' });
    }
  }

  return {
    timestamp,
    active,
    resolved,
    dbOps,
    totalServices: currentSummary.totalServicesListed || (currentSummary.services || []).length,
  };
}

module.exports = { processServices, toBKKTime, toBKKDateTime };
