/**
 * service.js
 * Collects a Downdetector BR service page via FlareSolverr (no browser).
 *
 * Target URL: https://downdetector.com.br/fora-do-ar/<slug>/
 * Current report count = last chart dataPoint.reportsValue.
 */

const { JSDOM } = require('jsdom');
const { fetchHtml } = require('./flaresolverr');
const { normalizeStatus } = require('./statusUtils');
const { formatBrasilia } = require('./timeBr');

const BASE_URL = 'https://downdetector.com.br/fora-do-ar/';
const DATA_POINTS_KEY = '\\"dataPoints\\":';

/**
 * Extract chart dataPoints array from page HTML (escaped Next.js payload).
 * @param {string} html
 * @returns {Array<{timestampUtc?: string, reportsValue?: number, baselineValue?: number}>}
 */
function extractDataPoints(html) {
  const keyIdx = html.indexOf(DATA_POINTS_KEY);
  if (keyIdx < 0) return [];

  const start = html.indexOf('[', keyIdx);
  if (start < 0) return [];

  let depth = 0;
  let end = start;
  for (let i = start; i < html.length; i++) {
    const ch = html[i];
    if (ch === '[') depth += 1;
    else if (ch === ']') {
      depth -= 1;
      if (depth === 0) {
        end = i + 1;
        break;
      }
    }
  }

  try {
    return JSON.parse(html.slice(start, end).replace(/\\"/g, '"'));
  } catch {
    return [];
  }
}

/**
 * Parse name / status / metrics from service HTML.
 * @param {string} html
 * @param {string} slug
 */
function parseServiceHtml(html, slug) {
  const dom = new JSDOM(html);
  const doc = dom.window.document;

  let rawStatus = null;
  let peakFromAria = null;
  const chartDiv =
    doc.querySelector('div[role="img"][aria-label*="Gráfico"]') ||
    doc.querySelector('div[role="img"][aria-label*="Reports chart"]');
  if (chartDiv) {
    const label = chartDiv.getAttribute('aria-label') || '';
    const m =
      label.match(/pico de (\d+) relatos?,\s*status:\s*(.+)$/i) ||
      label.match(/peak of (\d+) reports?,\s*status:\s*(.+)$/i);
    if (m) {
      peakFromAria = parseInt(m[1], 10);
      rawStatus = m[2].trim();
    }
  }

  let name = null;
  const h1 = doc.querySelector('h1');
  if (h1) {
    const text = h1.textContent.replace(/\s+/g, ' ').trim();
    const withMatch = text.match(/com\s+(.+)$/i);
    name = withMatch ? withMatch[1].trim() : text;
  }
  if (!name) {
    const titleMatch =
      doc.title.match(/^(.+?)\s+fora do ar/i) ||
      doc.title.match(/^(.+?)\s+status\b/i);
    if (titleMatch) name = titleMatch[1].trim();
  }
  if (!name) {
    const og = doc.querySelector('meta[property="og:title"]');
    if (og && og.content) {
      name = og.content.split(' fora do ar')[0].trim();
    }
  }

  const points = extractDataPoints(html);
  let reports = null;
  let reportsBaseline = null;
  let reportsAt = null;
  let peakReports24h = peakFromAria;

  if (points.length) {
    const last = points[points.length - 1];
    reports = last.reportsValue ?? null;
    reportsBaseline = last.baselineValue ?? null;
    reportsAt = formatBrasilia(last.timestampUtc);

    const peakFromSeries = points.reduce((max, p) => {
      const v = typeof p.reportsValue === 'number' ? p.reportsValue : 0;
      return v > max ? v : max;
    }, 0);
    if (peakFromSeries > 0) peakReports24h = peakFromSeries;
  }

  return {
    fetchedAt: formatBrasilia(new Date()),
    slug,
    name: name || slug,
    status: normalizeStatus(rawStatus),
    rawStatus,
    reports,
    reportsBaseline,
    reportsAt,
    peakReports24h,
    stale: false,
  };
}

/**
 * Scrape a service detail page via FlareSolverr.
 * @param {string} slug
 * @returns {Promise<Object>}
 */
async function scrapeService(slug) {
  const url = BASE_URL + slug + '/';
  console.log('[Service] Fetching via FlareSolverr:', url);
  const html = await fetchHtml(url);
  const data = parseServiceHtml(html, slug);
  if (data.reports == null && data.peakReports24h == null) {
    throw new Error('Parsed service HTML but found no report metrics');
  }
  return data;
}

module.exports = { scrapeService, extractDataPoints, parseServiceHtml };
