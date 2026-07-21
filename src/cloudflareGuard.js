/**
 * cloudflareGuard.js
 * Detects and waits out Cloudflare JS challenges.
 * Polls up to POLL_LIMIT times at POLL_INTERVAL_MS intervals.
 * Throws if still blocked after max attempts.
 */

const POLL_INTERVAL_MS = 2000;
const POLL_LIMIT = 15; // up to ~30s wait

/**
 * Returns true if the current page appears to be a Cloudflare challenge page.
 */
async function isCloudflarePage(page) {
  try {
    const title = await page.title();
    const t = title.toLowerCase();
    if (
      t.includes('just a moment') ||
      t.includes('um momento') ||
      t.includes('um segundo') ||
      t.includes('verifique') ||
      t === 'attention required! | cloudflare'
    ) {
      return true;
    }

    const hasCfChallenge = await page.evaluate(() => {
      const bodyText = (document.body && document.body.innerText) || '';
      return !!(
        document.querySelector('#challenge-running') ||
        document.querySelector('#cf-challenge-running') ||
        document.querySelector('cf-turnstile') ||
        document.querySelector('[data-cf-settings]') ||
        document.querySelector('#challenge-form') ||
        document.querySelector('.cf-browser-verification') ||
        document.title.toLowerCase().includes('just a moment') ||
        document.title.toLowerCase().includes('um momento') ||
        /checking your browser|verificando|enable javascript and cookies/i.test(bodyText)
      );
    });

    return hasCfChallenge;
  } catch {
    return false;
  }
}

/**
 * Waits until the Cloudflare challenge clears, or throws after timeout.
 * Call this after every page.goto().
 *
 * @param {import('puppeteer').Page} page
 */
async function ensurePassedChallenge(page) {
  for (let attempt = 0; attempt < POLL_LIMIT; attempt++) {
    const blocked = await isCloudflarePage(page);
    if (!blocked) {
      console.log('[CF Guard] Page OK — no challenge detected.');
      return;
    }
    console.log(
      `[CF Guard] Cloudflare challenge detected — waiting ${POLL_INTERVAL_MS}ms (attempt ${attempt + 1}/${POLL_LIMIT})...`
    );
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
  }

  throw new Error(
    '[CF Guard] Cloudflare challenge did not clear after maximum wait time. ' +
    'Try running with HEADLESS=false to allow manual interaction, or configure a proxy.'
  );
}

module.exports = { ensurePassedChallenge, isCloudflarePage };
