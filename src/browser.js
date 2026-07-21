/**
 * browser.js
 * Stealth-hardened Puppeteer browser launcher.
 * Supports EXEC_PATH env var for custom Chrome/Chromium binary.
 * Supports USER_DATA_DIR env var for persistent profile (cookie reuse).
 * Supports HEADLESS env var (set to "true" to go headless, default: false).
 * Supports PROXY_URL env var (e.g. "http://user:pass@proxy:8080").
 */

const fs       = require('fs');
const path     = require('path');
const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');

puppeteer.use(StealthPlugin());

const DEFAULT_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36';

async function launchBrowser() {
  const executablePath =
    process.env.EXEC_PATH ||
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

  const userDataDir =
    process.env.USER_DATA_DIR || './crawler-profile';

  const headless =
    process.env.HEADLESS === 'true' ? 'new' : false;

  // Chrome leaves a SingletonLock file when it crashes or is killed.
  // If present it prevents a new instance from using the same profile,
  // causing Puppeteer to fail with "Failed to launch the browser process!".
  const lockFile = path.join(userDataDir, 'SingletonLock');
  if (fs.existsSync(lockFile)) {
    console.warn('[Browser] Stale SingletonLock found — removing before launch');
    try { fs.unlinkSync(lockFile); } catch (e) {
      console.warn('[Browser] Could not remove SingletonLock:', e.message);
    }
  }

  const args = [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--disable-infobars',
    '--disable-blink-features=AutomationControlled',
    '--window-size=1366,768',
    '--lang=pt-BR',
  ];

  if (process.env.PROXY_URL) {
    args.push(`--proxy-server=${process.env.PROXY_URL}`);
  }

  const browser = await puppeteer.launch({
    headless,
    executablePath,
    userDataDir,
    defaultViewport: { width: 1366, height: 768 },
    args,
  });

  return browser;
}

/**
 * Create a new page with realistic headers applied.
 */
async function newPage(browser) {
  const page = await browser.newPage();

  await page.setUserAgent(DEFAULT_UA);
  await page.setExtraHTTPHeaders({
    'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
  });

  // Extra hardening: mask navigator.webdriver even beyond stealth plugin
  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  });

  return page;
}

module.exports = { launchBrowser, newPage };
