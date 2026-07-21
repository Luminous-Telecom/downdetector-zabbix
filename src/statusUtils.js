/**
 * statusUtils.js
 * Shared status normalisation helper used by homepage.js and service.js.
 */

/**
 * Normalize a raw Downdetector status string → internal enum value.
 *
 * Supports PT-BR (downdetector.com.br) and English fallbacks.
 *
 * @param {string|null} raw - e.g. "sem problemas", "Possíveis problemas", "no problems"
 * @returns {'OK'|'WARNING'|'DOWN'|string} Uppercase enum string
 */
function normalizeStatus(raw) {
  const s = (raw || '')
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .trim();

  // Order matters: "possiveis problemas" contains "problemas"
  if (s.includes('possiv') || s.includes('possible')) {
    return 'WARNING';
  }
  if (s.includes('sem problemas') || s === 'no problems') {
    return 'OK';
  }
  if (
    s.includes('major') ||
    s === 'problema' ||
    s === 'problemas' ||
    s === 'problem' ||
    s.includes('outage') ||
    s.includes('experiencing') ||
    /\bproblemas?\b/.test(s)
  ) {
    return 'DOWN';
  }

  return s.toUpperCase();
}

module.exports = { normalizeStatus };
