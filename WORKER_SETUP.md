# Cloudflare Worker Health-Check Setup Guide

The `worker/` directory contains a standalone Cloudflare Worker that monitors the
Downdetector scraper. It runs every **20 minutes** via cron, checks the D1 `summaries`
table, and sends a **Power Automate alert** if the scraper hasn't reported in over 50 minutes.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Node.js ≥ 18 | For running Wrangler |
| Wrangler CLI | `npm install -g wrangler` |
| Cloudflare account | Same account that owns the D1 database |
| D1 database | `downdetector-alarm` — already created by the scraper |
| Power Automate webhook | A **separate** HTTP-trigger flow just for health alerts |

---

## Step 1 — Create a Separate Power Automate Flow

> **Why separate?** The health-check alert has its own format and should route to a dedicated
> channel/person, separate from the regular service-down alerts.

1. Go to [make.powerautomate.com](https://make.powerautomate.com)
2. Create a new **Instant cloud flow** → trigger: **When an HTTP request is received**
3. Add action: **Post message in a chat or channel** (MS Teams)
   - In the Message field use: `@{triggerBody()?['htmlMessage']}`
4. Save the flow and copy the **HTTP POST URL** from the trigger card
5. Keep this URL — you'll use it in Step 4 below

---

## Step 2 — Install Worker Dependencies

```powershell
cd worker
npm install
```

---

## Step 3 — Authenticate Wrangler

If you haven't already logged into Wrangler:

```powershell
npx wrangler login
```

This opens a browser to authenticate with your Cloudflare account.

---

## Step 4 — Set the Webhook Secret

The Worker receives the webhook URL as a **Cloudflare secret** (never stored in plain text):

```powershell
# Run from the worker/ directory
npx wrangler secret put HEALTH_CHECK_WEBHOOK_URL
```

When prompted, paste the Power Automate HTTP POST URL from Step 1.

---

## Step 5 — Verify the D1 Binding

The `wrangler.toml` already has the D1 binding configured:

```toml
[[d1_databases]]
binding = "DB"
database_name = "downdetector-alarm"
database_id = "a4696e2f-3e65-40b1-a168-5fd957e593ad"
```

Confirm the `database_id` matches your actual D1 database:

```powershell
npx wrangler d1 list
```

Update `wrangler.toml` if the ID is different.

---

## Step 6 — Test Locally

Start the Worker in local development mode:

```powershell
# From the worker/ directory
npm run dev
# or: npx wrangler dev
```

Then trigger the health check manually:

```powershell
curl http://localhost:8787/trigger
```

Check the Wrangler dev console for output like:

```
[HealthCheck] Cron fired at 2026-07-14T08:30:00.000Z
[HealthCheck] Last scrape: 2026-07-14T08:15:00.000Z (15 min ago, threshold: 50 min)
[HealthCheck] Scraper is healthy — no alert needed.
```

To **force an alert** (simulate a downed server), temporarily change `STALE_THRESHOLD_MIN`
to `1` in `index.js`, trigger again, then revert.

---

## Step 7 — Deploy to Cloudflare

```powershell
# From the worker/ directory
npm run deploy
# or: npx wrangler deploy
```

Expected output:

```
Uploaded downdetector-health-check (X.XX sec)
Published downdetector-health-check (X.XX sec)
  https://downdetector-health-check.<your-subdomain>.workers.dev
Current Cron Triggers:
  */20 * * * *
```

---

## Step 8 — Verify the Cron is Active

In the **Cloudflare Dashboard**:

1. Go to **Workers & Pages** → select `downdetector-health-check`
2. Click **Triggers** tab
3. Confirm the cron `*/20 * * * *` is listed

---

## Monitoring Worker Logs

Stream live Worker logs:

```powershell
npm run tail
# or: npx wrangler tail
```

Or view logs in the dashboard: **Workers & Pages** → `downdetector-health-check` → **Logs**

---

## Alert Message Example

When the scraper goes down, Teams will receive:

```
🚨 Downdetector Scraper — Server Down Alert
2026-07-14 15:30:00 (UTC+7)

Status: ❌ SCRAPER SERVER NOT RESPONDING
Last successful scrape: 2026-07-14 14:30:00 (UTC+7)
Time since last scrape: 60 minutes
Alert threshold: 50 minutes

⚠️ Action Required:
1. SSH / RDP into the scraper server
2. Check if the Node.js process is still running:
   ps aux | grep node     (Linux)
   Get-Process node       (Windows PowerShell)
3. Inspect recent logs:
   journalctl -u downdetector-scraper -n 50   (systemd)
   — or check your PM2/screen/nssm logs
4. Restart the scraper if it has stopped:
   npm start   (from the project directory)
5. If it keeps crashing, look for:
   • Out-of-memory kills (check dmesg | grep -i oom)
   • Disk full (df -h)
   • FlareSolverr offline (check FLARESOLVERR_URL)
   • Cloudflare D1 / R2 credential expiry
```

---

## Configuration Summary

| Setting | Where | Value |
|---|---|---|
| Cron schedule | `worker/wrangler.toml` | `*/20 * * * *` (every 20 min) |
| Stale threshold | `worker/index.js` | `50` minutes |
| D1 database | `worker/wrangler.toml` | `downdetector-alarm` |
| Webhook URL | Wrangler secret | Set via `wrangler secret put` |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `HEALTH_CHECK_WEBHOOK_URL secret is not set` | Run `wrangler secret put HEALTH_CHECK_WEBHOOK_URL` |
| `D1 binding "DB" is missing` | Check `[[d1_databases]]` in `worker/wrangler.toml` |
| `No rows in summaries table` | Scraper hasn't run yet — start it with `npm start` in the root dir |
| Alert fires but no Teams message | Check the PA flow is enabled and the message body uses `triggerBody()?['htmlMessage']` |
| Cron not listed in Triggers tab | Re-deploy: `wrangler deploy` |
