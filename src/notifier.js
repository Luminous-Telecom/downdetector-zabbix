/**
 * notifier.js
 * Builds the HTML-formatted Teams message and POSTs it to
 * the Power Automate webhook.
 *
 * Power Automate flow:
 *   Trigger: "When a HTTP request is received" (POST)
 *   Action:  "Post message in a chat or channel"
 *            → Message body field: @{triggerBody()?['htmlMessage']}
 *
 * Required env var:
 *   POWER_AUTOMATE_WEBHOOK_URL — HTTP POST URL from the PA trigger
 *
 * Message format per service:
 *   Service Name  (bold, hyperlinked to downdetector.com.br/fora-do-ar/{slug})
 *   Round: N
 *   Status: 🔴 Critical | 🟡 Warning | 🟢 Clear | 🟣 Not Found
 *   Start: HH:MM    End: HH:MM  (or "-" if still active)
 *   Duration: X minutes          (only shown when resolved)
 *   Screenshot: embedded <img src="https://r2-url"> (if available)
 */

const DD_BASE_URL = "https://downdetector.com.br/fora-do-ar";

// ---------------------------------------------------------------------------
// Status display helper
// ---------------------------------------------------------------------------

function statusLabel(status) {
  if (status === 'DOWN')      return '<span style="color:#e81123">Critical</span>';
  if (status === 'WARNING')   return '<span style="color:#ff8c00">Warning</span>';
  if (status === 'NOT_FOUND') return '<span style="color:#7b1fa2">Data not found</span>';
  return '<span style="color:#107c41">Clear</span>';
}

// ---------------------------------------------------------------------------
// HTML message builder
// ---------------------------------------------------------------------------

/**
 * Build the HTML message body for MS Teams.
 *
 * Teams chat supports a limited HTML subset:
 *   <b>, <i>, <a href>, <br>, <img src>
 *   Inline CSS color is NOT supported in chat messages.
 *   Color is conveyed via emoji (🔴🟡🟢).
 *
 * @param {Object} result — output from processServices()
 * @param {Map<string,string>} [screenshotUrls] — Map<slug, publicR2Url>
 * @returns {string} HTML string
 */
function buildHtmlMessage(result, screenshotUrls = new Map()) {
  const { timestamp, active, resolved } = result;

  const lines = [];

  lines.push('<b>Downdetector BR Alert</b>');
  lines.push(timestamp);

  const allServices = [...active, ...resolved];

  // --- Pass 1: all service text blocks ---
  for (const svc of allServices) {
    lines.push('');
    lines.push(
      `Service: <b><a href="${DD_BASE_URL}/${svc.slug}">${svc.name}</a></b>`,
    );
    lines.push(`Round: ${svc.round}`);
    lines.push(`Status: ${statusLabel(svc.status)}`);
    lines.push(`Start: ${svc.startTimeStr}`);
    if (svc.endTimeStr) {
      lines.push(`End: ${svc.endTimeStr}`);
    }
    if (svc.endTimeStr && svc.durationMin != null) {
      lines.push(
        `Duration: ${svc.durationMin} minute${svc.durationMin !== 1 ? 's' : ''}`,
      );
    }
  }

  // --- Pass 2: all screenshots grouped at the bottom ---
  const imgLines = allServices
    .filter((svc) => screenshotUrls.has(svc.slug))
    .map((svc) => {
      const imgUrl = screenshotUrls.get(svc.slug);
      return `<img src="${imgUrl}" alt="${svc.name} screenshot" width="400"/>`;
    });

  if (imgLines.length > 0) {
    // lines.push('<b>--- Graphs ---</b>');
    lines.push(imgLines.join(' '));
  }

  return lines.join('<br>') + '<br>';
}

// ---------------------------------------------------------------------------
// Webhook POST
// ---------------------------------------------------------------------------

/**
 * Send the process result to Power Automate.
 * Gracefully skips if POWER_AUTOMATE_WEBHOOK_URL is not configured.
 *
 * @param {Object} result           — output from processServices()
 * @param {Map<string,string>} [screenshotUrls] — Map<slug, publicR2Url> from uploadScreenshots()
 */
async function sendTeamsNotification(result, screenshotUrls = new Map()) {
  const webhookUrl = process.env.POWER_AUTOMATE_WEBHOOK_URL;

  if (!webhookUrl) {
    return; // opcional — sem webhook, silencioso
  }

  // Only send notification if there are warning/critical incidents OR resolved ones
  const hasIssues = result.active.length > 0 || result.resolved.length > 0;
  if (!hasIssues) {
    console.log(
      "[Notifier] No active or resolved issues — skipping Teams notification",
    );
    return;
  }

  const htmlMessage = buildHtmlMessage(result, screenshotUrls);

  const payload = {
    timestamp: result.timestamp,
    totalServices: result.totalServices,
    hasIssues: true,
    htmlMessage,
    active: result.active,
    resolved: result.resolved,
  };

  const res = await fetch(webhookUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const body = await res.text().catch(() => '');
    let hint = '';
    if (res.status === 404) {
      hint =
        '\n  Guidance: Webhook URL returned 404 — the URL may have changed or the PA flow was deleted.\n' +
        '  1. Go to Power Automate → My Flows → find the flow → copy the fresh HTTP POST URL.\n' +
        '  2. Update POWER_AUTOMATE_WEBHOOK_URL in .env.';
    } else if (res.status === 401 || res.status === 403) {
      hint =
        '\n  Guidance: Webhook returned auth error — the flow trigger URL may have expired.\n' +
        '  1. Regenerate the trigger URL in Power Automate → trigger card → "Generate new URL".\n' +
        '  2. Update POWER_AUTOMATE_WEBHOOK_URL in .env.';
    } else if (res.status >= 500) {
      hint =
        '\n  Guidance: Power Automate server error — this is usually temporary.\n' +
        '  1. Check https://status.microsoft.com for Power Automate outages.\n' +
        '  2. Retry in a few minutes; next cron run will re-attempt.';
    } else {
      hint =
        '\n  Guidance:\n' +
        '  1. Verify the flow trigger action is "When an HTTP request is received" and is enabled.\n' +
        '  2. Confirm POWER_AUTOMATE_WEBHOOK_URL is the full URL including the SAS token.\n' +
        '  3. Check if the PA environment or solution has restrictions on external HTTP triggers.';
    }
    throw new Error(
      `[Notifier] Webhook returned ${res.status}: ${body.slice(0, 300)}${hint}`
    );
  }

  console.log(`[Notifier] Webhook delivered (${res.status})`);
}

module.exports = { sendTeamsNotification, buildHtmlMessage };
