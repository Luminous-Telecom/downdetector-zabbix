/**
 * homepage.js
 * Collects Downdetector BR homepage summary via FlareSolverr (no browser).
 *
 * Target URL: https://downdetector.com.br/
 */

const { JSDOM } = require('jsdom');
const { fetchHtml } = require('./flaresolverr');
const { normalizeStatus } = require('./statusUtils');
const { formatBrasilia } = require('./timeBr');

const BASE_URL = 'https://downdetector.com.br/';

/**
 * Parse homepage HTML into a summary object.
 * @param {string} html
 */
function parseHomepageHtml(html) {
  const dom = new JSDOM(html);
  const doc = dom.window.document;

  const lists = [...doc.querySelectorAll('ul.contents')];
  const list = lists.find((el) => {
    const label = (el.getAttribute('aria-label') || '').toLowerCase();
    return label.includes('servi') || label.includes('service');
  });

  if (!list) {
    throw new Error('Services list not found in DOM');
  }

  const countMatch = (list.getAttribute('aria-label') || '').match(/(\d+)/);
  const totalServicesListed = countMatch ? parseInt(countMatch[1], 10) : null;

  const services = [];
  const cards = list.querySelectorAll('div[data-testid^="card-company-"]');

  cards.forEach((card) => {
    const testId = card.getAttribute('data-testid') || '';
    const idMatch = testId.match(/card-company-(\d+)/);
    const companyId = idMatch ? idMatch[1] : null;

    const anchor = card.querySelector('a[href*="/fora-do-ar/"]');
    let slug = null;
    let name = null;
    if (anchor) {
      const href = anchor.getAttribute('href') || '';
      const m = href.match(/\/fora-do-ar\/([^/]+)/);
      slug = m ? m[1] : null;
      const ariaLabel = anchor.getAttribute('aria-label') || '';
      name =
        ariaLabel.replace(/^Página de status\s*/i, '').trim() ||
        (card.querySelector('h2') || {}).textContent?.trim() ||
        null;
    }

    const imgDiv = card.querySelector('div[role="img"][aria-label]');
    let rawStatus = null;
    if (imgDiv) {
      const label = imgDiv.getAttribute('aria-label') || '';
      const m =
        label.match(/Status atual:\s*(.+)$/i) ||
        label.match(/Current status:\s*(.+)$/i);
      rawStatus = m ? m[1].trim() : null;
    }

    let logo = null;
    const logoImg = card.querySelector('img[src*="logo"], img[src*="static/uploads"]');
    if (logoImg) {
      const src = logoImg.getAttribute('src') || '';
      // Prefer the original CDN logo URL when wrapped by cdn-cgi/image
      const nested = src.match(/https?:\/\/cdn\d*\.downdetector\.com\/static\/uploads\/logo\/[^?\s]+/i);
      logo = nested ? nested[0] : src || null;
    }

    services.push({
      companyId,
      name,
      slug,
      status: normalizeStatus(rawStatus),
      rawStatus,
      logo,
    });
  });

  return {
    fetchedAt: formatBrasilia(new Date()),
    totalServicesListed: totalServicesListed || services.length,
    services,
    stale: false,
  };
}

/**
 * Scrape homepage via FlareSolverr API.
 * @returns {Promise<Object>}
 */
async function scrapeHomepage() {
  console.log('[Homepage] Fetching via FlareSolverr:', BASE_URL);
  const html = await fetchHtml(BASE_URL);
  return parseHomepageHtml(html);
}

module.exports = { scrapeHomepage, parseHomepageHtml };
