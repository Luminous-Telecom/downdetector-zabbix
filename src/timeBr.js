/**
 * timeBr.js — Brasília (America/Sao_Paulo) formatting for Downdetector BR.
 */

const TZ_BR = 'America/Sao_Paulo';

/**
 * Format like Downdetector chart labels: "21/07/2026, 5:58 PM"
 * @param {string|Date|null|undefined} input
 * @returns {string|null}
 */
function formatBrasilia(input) {
  if (!input) return null;
  const date = input instanceof Date ? input : new Date(input);
  if (Number.isNaN(date.getTime())) return null;

  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: TZ_BR,
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  }).formatToParts(date);

  const get = (type) => parts.find((p) => p.type === type)?.value || '';
  return `${get('day')}/${get('month')}/${get('year')}, ${get('hour')}:${get('minute')} ${get('dayPeriod')}`;
}

module.exports = { formatBrasilia, TZ_BR };
