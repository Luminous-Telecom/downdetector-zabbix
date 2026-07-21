/**
 * r2Uploader.js
 *
 * Uploads PNG screenshot Buffers to Cloudflare R2 via the S3-compatible API
 * and returns a Map<slug, publicUrl>.
 *
 * Object key format:
 *   service-graph-captures/YYYY-MM-DD/HH-MM-SS_<slug>.png
 *
 * Required env vars:
 *   R2_ACCESS_KEY_ID      — R2 API Token (Access Key ID)
 *   R2_SECRET_ACCESS_KEY  — R2 API Token (Secret Access Key)
 *   R2_BUCKET_NAME        — R2 bucket name
 *   R2_ENDPOINT           — https://<CLOUDFLARE_ACCOUNT_ID>.r2.cloudflarestorage.com
 *   R2_PUBLIC_BASE_URL    — Public base URL, e.g. https://pub-xxxx.r2.dev
 *
 * Non-fatal: returns an empty Map and logs a warning if any env var is missing.
 */

const { S3Client, PutObjectCommand, ListObjectsV2Command, DeleteObjectsCommand } = require('@aws-sdk/client-s3');

// ---------------------------------------------------------------------------
// Lazy-initialised S3 client (created once on first upload)
// ---------------------------------------------------------------------------

let _client = null;

function getClient() {
  if (_client) return _client;

  const {
    R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY,
    R2_ENDPOINT,
  } = process.env;

  _client = new S3Client({
    region: 'auto',
    endpoint: R2_ENDPOINT,
    credentials: {
      accessKeyId: R2_ACCESS_KEY_ID,
      secretAccessKey: R2_SECRET_ACCESS_KEY,
    },
  });

  return _client;
}

// ---------------------------------------------------------------------------
// Config validation
// ---------------------------------------------------------------------------

function r2Configured() {
  const required = [
    'R2_ACCESS_KEY_ID',
    'R2_SECRET_ACCESS_KEY',
    'R2_BUCKET_NAME',
    'R2_ENDPOINT',
    'R2_PUBLIC_BASE_URL',
  ];
  const missing = required.filter((k) => !process.env[k]);
  if (missing.length > 0) {
    console.warn(
      `[R2Uploader] Missing env vars: ${missing.join(', ')} — skipping R2 upload`,
    );
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Object key builder
// ---------------------------------------------------------------------------

/**
 * Build the R2 object key for a given slug and timestamp.
 *
 * @param {string} slug
 * @param {Date}   ts
 * @returns {string}  e.g. "service-graph-captures/2026-07-14/10-05-32_gmail.png"
 */
function buildKey(slug, ts) {
  const date = ts.toISOString().slice(0, 10); // YYYY-MM-DD
  const time = ts.toISOString().slice(11, 19).replace(/:/g, '-'); // HH-MM-SS
  return `service-graph-captures/${date}/${time}_${slug}.png`;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Upload PNG screenshots to Cloudflare R2.
 *
 * @param {Map<string, Buffer>} screenshots  — Map from slug → raw PNG Buffer
 * @returns {Promise<Map<string, string>>}   — Map from slug → public HTTPS URL
 */
async function uploadScreenshots(screenshots) {
  if (!screenshots || screenshots.size === 0) return new Map();

  if (!r2Configured()) return new Map();

  const bucket = process.env.R2_BUCKET_NAME;
  const publicBase = process.env.R2_PUBLIC_BASE_URL.replace(/\/$/, '');
  const client = getClient();
  const urls = new Map();
  const ts = new Date();

  for (const [slug, buf] of screenshots) {
    const key = buildKey(slug, ts);

    try {
      await client.send(
        new PutObjectCommand({
          Bucket: bucket,
          Key: key,
          Body: buf,
          ContentType: 'image/png',
          CacheControl: 'public, max-age=86400',
        }),
      );

      const url = `${publicBase}/${key}`;
      urls.set(slug, url);
      console.log(`[R2Uploader] ✓ "${slug}" → ${url}`);
    } catch (err) {
      // Non-fatal: log and continue; notification will send without this image
      console.warn(`[R2Uploader] ✗ Failed to upload "${slug}":`, err.message);
    }
  }

  console.log(
    `[R2Uploader] Done — ${urls.size}/${screenshots.size} screenshot(s) uploaded`,
  );
  return urls;
}

// ---------------------------------------------------------------------------
// Cleanup — delete objects older than N days
// ---------------------------------------------------------------------------

/**
 * Delete all objects under service-graph-captures/ whose date-folder
 * (YYYY-MM-DD) is older than `maxAgeDays` (default: 7).
 *
 * Non-fatal: logs a warning on any error.
 *
 * @param {number} [maxAgeDays=7]
 * @returns {Promise<number>} number of objects deleted
 */
async function purgeOldScreenshots(maxAgeDays = 7) {
  if (!r2Configured()) return 0;

  const bucket = process.env.R2_BUCKET_NAME;
  const client = getClient();
  const prefix = 'service-graph-captures/';

  // Cutoff: midnight N days ago (UTC)
  const cutoff = new Date();
  cutoff.setUTCDate(cutoff.getUTCDate() - maxAgeDays);
  cutoff.setUTCHours(0, 0, 0, 0);
  const cutoffStr = cutoff.toISOString().slice(0, 10); // YYYY-MM-DD

  console.log(`[R2Purge] Deleting objects under "${prefix}" older than ${cutoffStr}`);

  const toDelete = [];
  let continuationToken;

  // Page through all objects under the prefix
  do {
    const listResp = await client.send(
      new ListObjectsV2Command({
        Bucket: bucket,
        Prefix: prefix,
        ContinuationToken: continuationToken,
      }),
    );

    for (const obj of listResp.Contents || []) {
      // Key format: service-graph-captures/YYYY-MM-DD/HH-MM-SS_slug.png
      const dateFolder = obj.Key.split('/')[1]; // "YYYY-MM-DD"
      if (dateFolder && dateFolder < cutoffStr) {
        toDelete.push({ Key: obj.Key });
      }
    }

    continuationToken = listResp.IsTruncated ? listResp.NextContinuationToken : undefined;
  } while (continuationToken);

  if (toDelete.length === 0) {
    console.log('[R2Purge] No stale objects found.');
    return 0;
  }

  // R2 / S3 allows max 1000 keys per DeleteObjects request
  const BATCH = 1000;
  let deleted = 0;
  for (let i = 0; i < toDelete.length; i += BATCH) {
    const batch = toDelete.slice(i, i + BATCH);
    await client.send(
      new DeleteObjectsCommand({
        Bucket: bucket,
        Delete: { Objects: batch, Quiet: true },
      }),
    );
    deleted += batch.length;
  }

  console.log(`[R2Purge] Deleted ${deleted} stale object(s).`);
  return deleted;
}

module.exports = { uploadScreenshots, purgeOldScreenshots };
