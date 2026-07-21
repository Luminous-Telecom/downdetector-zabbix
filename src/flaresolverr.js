/**
 * flaresolverr.js
 * HTTP fetch via FlareSolverr (no local browser).
 * Aceita apenas endpoint em loopback (127.0.0.1 / localhost).
 */

const FLARESOLVERR_URL =
  process.env.FLARESOLVERR_URL || 'http://127.0.0.1:8191/v1';

function assertLocalFlareSolverr(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error(
      `FLARESOLVERR_URL inválida: ${url}. Use http://127.0.0.1:8191/v1`
    );
  }
  const host = parsed.hostname.toLowerCase();
  if (host !== '127.0.0.1' && host !== 'localhost' && host !== '::1') {
    throw new Error(
      `FLARESOLVERR_URL deve apontar só para localhost (recebido: ${host}). ` +
        `Use http://127.0.0.1:8191/v1 e publique o Docker com -p 127.0.0.1:8191:8191`
    );
  }
}

assertLocalFlareSolverr(FLARESOLVERR_URL);

/**
 * Fetch a URL through FlareSolverr and return HTML.
 * @param {string} url
 * @param {number} [maxTimeoutMs=90000]
 * @returns {Promise<string>}
 */
async function fetchHtml(url, maxTimeoutMs = 90000) {
  const resp = await fetch(FLARESOLVERR_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      cmd: 'request.get',
      url,
      maxTimeout: maxTimeoutMs,
    }),
  });

  if (!resp.ok) {
    throw new Error(`FlareSolverr HTTP ${resp.status}`);
  }

  const data = await resp.json();
  if (data.status !== 'ok') {
    throw new Error(data.message || 'FlareSolverr error');
  }

  const sol = data.solution || {};
  if (sol.status && sol.status !== 200) {
    throw new Error(`FlareSolverr upstream HTTP ${sol.status}`);
  }

  const html = sol.response || '';
  if (!html || html.length < 1000) {
    throw new Error('FlareSolverr returned empty/short HTML');
  }

  if (
    /just a moment|um momento|cf-chl|challenge-platform/i.test(html) &&
    !/dataPoints|card-company|fora-do-ar/i.test(html)
  ) {
    throw new Error('FlareSolverr still hit Cloudflare challenge');
  }

  return html;
}

module.exports = { fetchHtml, FLARESOLVERR_URL };
