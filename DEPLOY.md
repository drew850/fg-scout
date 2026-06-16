# Scout — Deploy Instructions

## Files to push to GitHub
- server.py
- scout.html
- requirements.txt
- Procfile

---

## Step 1 — Create GitHub repo (2 min)

1. Go to github.com → New repository
2. Name: `scout` (or `freebird-scout`)
3. Private: yes
4. Don't initialize with README
5. Push your files:

```
git init
git add .
git commit -m "Scout v1.0.0"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/scout.git
git push -u origin main
```

---

## Step 2 — Create Railway service (3 min)

1. Go to railway.app → New Project → Deploy from GitHub repo
2. Select your `scout` repo
3. Railway will detect Python and deploy automatically
4. Once deployed, copy the Railway URL (e.g. `https://scout-production-xxxx.up.railway.app`)

---

## Step 3 — Add Postgres to Railway (2 min)

1. In your Railway project → New → Database → PostgreSQL
2. Railway auto-links `DATABASE_URL` to your service — no manual config needed

---

## Step 4 — Set environment variables (5 min)

In Railway → your Scout service → Variables, add:

| Variable | Value |
|---|---|
| `BASE_URL` | Your Railway URL (e.g. `https://scout-production-xxxx.up.railway.app`) |
| `GOOGLE_CLIENT_ID` | From your new Google OAuth app (see Step 5) |
| `GOOGLE_CLIENT_SECRET` | From your new Google OAuth app (see Step 5) |
| `ANTHROPIC_KEY` | Same key used in Wingman |
| `GORGIAS_DOMAIN` | `freedomgrooming.gorgias.com` |
| `GORGIAS_USERNAME` | `drew@myfreebird.com` |
| `GORGIAS_API_KEY` | From Gorgias REST API settings page |
| `ADMIN_SEED_EMAIL` | `drew@myfreebird.com` |
| `EMERGENCY_PIN` | Choose a strong PIN (e.g. 12 random characters) |

Note: `DATABASE_URL` is set automatically by Railway when you add Postgres.

---

## Step 5 — Create Google OAuth app (5 min)

1. Go to console.cloud.google.com
2. Create a new project: "Scout"
3. APIs & Services → OAuth consent screen
   - User type: Internal
   - App name: Scout
   - User support email: drew@myfreebird.com
   - Authorized domain: myfreebird.com
   - Save
4. APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: Web application
   - Name: Scout
   - Authorized redirect URIs → Add:
     `https://YOUR_RAILWAY_URL/auth/callback`
   - Create
5. Copy the Client ID and Client Secret → paste into Railway env vars

---

## Step 6 — Set up Railway Cron services (5 min)

You need two separate Cron services in Railway.

### Cron 1 — Daily sync (12:00 AM MNL = 16:00 UTC)

1. Railway project → New → Cron Job
2. Schedule: `0 16 * * *`
3. Command: `curl -X POST https://YOUR_RAILWAY_URL/api/cron/sync`

### Cron 2 — Weekly insights (Tuesday 6:00 PM MNL = 10:00 UTC)

1. Railway project → New → Cron Job
2. Schedule: `0 10 * * 2`
3. Command: `curl -X POST https://YOUR_RAILWAY_URL/api/cron/insights`

---

## Step 7 — Verify deploy (2 min)

1. Open your Railway URL in the browser
2. You should see the Scout login screen
3. Click "Sign in with Google" — log in with drew@myfreebird.com
4. You should land on the Dashboard
5. Go to Data tab → check sync status
6. The backfill starts automatically on first deploy (pulls from May 1, 2026)
   - This runs in the background, may take 5–10 minutes
   - Watch Railway logs for `[Sync] Done — X tickets synced`

---

## Step 8 — Add users (2 min)

1. Go to Settings tab (only visible to admin)
2. Add each team member's work email
3. Assign role (viewer for dept heads, admin only for yourself)
4. They can log in immediately with their @myfreebird.com Google account

---

## Ongoing maintenance

| Task | When | How |
|---|---|---|
| Daily sync | Automatic | Railway Cron at 12 AM MNL |
| Weekly insights | Automatic | Railway Cron Tuesday 6 PM MNL |
| Manual sync | Anytime | Data tab → Run sync |
| Refresh insights | Anytime | Departments tab → Refresh insights |
| Add/remove users | Anytime | Settings tab |
| Redeploy after code changes | On push | Railway auto-deploys from GitHub main |

---

## Troubleshooting

**Login fails with "auth_failed"**
→ Check GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are correct in Railway
→ Confirm the redirect URI in Google Cloud Console exactly matches BASE_URL + `/auth/callback`

**Backfill not running**
→ Check Railway logs for `[Backfill]` messages
→ Check GORGIAS_USERNAME and GORGIAS_API_KEY are set
→ Can manually trigger from Data tab → Re-run backfill

**No insights generating**
→ Check ANTHROPIC_KEY is set
→ Manually trigger from Departments tab → Refresh insights
→ Check Railway logs for `[Insights]` messages

**Database errors**
→ Confirm Postgres is linked to your Railway service
→ DATABASE_URL should be auto-populated — check Variables tab
