/**
 * cardCapture.js
 *
 * Captures a PNG screenshot of each alerted service card element on
 * the already-loaded Downdetector homepage, returning a Map<slug, base64String>.
 *
 * Requirements:
 *   • The Puppeteer `page` must still be open on the homepage (before page.close()).
 *   • Each entry in `activeServices` must have a `companyId` field (from homepage.js).
 *
 * No new npm dependencies — uses Puppeteer's built-in page.screenshot() + boundingBox().
 */

/**
 * Capture one PNG screenshot per alerted service card.
 *
 * @param {import('puppeteer').Page} page        - Open Puppeteer page (homepage still loaded)
 * @param {Array<{slug: string, companyId: string|null}>} activeServices - Active alert entries
 * @returns {Promise<Map<string, Buffer>>}         - Map from slug → raw PNG Buffer
 */
async function captureAlertedCards(page, activeServices) {
  const screenshots = new Map(); // slug → raw PNG Buffer

  for (const svc of activeServices) {
    if (!svc.companyId) {
      console.warn(
        `[CardCapture] No companyId for "${svc.slug}" — skipping screenshot`,
      );
      continue;
    }

    const selector = `[data-testid="card-company-${svc.companyId}"]`;

    try {
      const el = await page.$(selector);
      if (!el) {
        console.warn(
          `[CardCapture] Card element not found for "${svc.slug}" (${selector})`,
        );
        continue;
      }

      const buf = await el.screenshot({
        type: "png",
      });

      screenshots.set(svc.slug, buf);
      console.log(
        `[CardCapture] ✓ "${svc.slug}" — ${Math.round(buf.length / 1024)} KB PNG captured`,
      );
    } catch (err) {
      // Non-fatal: log and continue; notification will still send without this image
      console.warn(
        `[CardCapture] ✗ Failed to capture "${svc.slug}":`,
        err.message,
      );
    }
  }

  console.log(
    `[CardCapture] Done — ${screenshots.size}/${activeServices.length} screenshot(s) captured`,
  );
  return screenshots;
}

module.exports = { captureAlertedCards };
