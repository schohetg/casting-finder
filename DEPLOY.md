# Casting Finder — Deployment Guide

## Quickest Way: Deploy to Render.com (Free, Persistent)

### Step 1 — Put the code on GitHub
1. Create a free account at github.com
2. Create a new repository called `casting-finder` (private is fine)
3. Upload all these files to the repo (drag & drop works in the GitHub web UI)

### Step 2 — Deploy on Render
1. Create a free account at render.com
2. Click **New → Web Service**
3. Connect your GitHub repo
4. Render will detect the `render.yaml` file automatically. Click **Apply**
5. In the service settings, go to **Disks** and confirm the `/data` disk is attached (1 GB — this is where your database lives permanently)
6. Click **Deploy** — takes ~2 minutes

That's it. Render gives you a URL like `https://casting-finder-xxxx.onrender.com`.

**Important:** On the free Render tier, the app "sleeps" after 15 minutes of inactivity. The first load after sleeping takes ~30 seconds. To avoid this, upgrade to the Starter plan ($7/month) or use a free uptime monitor like UptimeRobot to ping the URL every 10 minutes.

---

## Alternative: Railway.app

1. Create account at railway.app
2. New Project → Deploy from GitHub repo
3. Add a **Volume** at `/data` in the service settings
4. Add environment variable: `DB_PATH = /data/castings.db`
5. Railway auto-detects the `Procfile`

---

## Local Testing (Mac)

```bash
cd casting-finder

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the app
python app.py
```

Then open http://localhost:5000 in your browser.

---

## How to Add a New Casting Site

Open `scraper.py` and copy any existing scraper class (e.g. `StudentFilmScraper`).

Change:
- `name` = the site's domain
- `URLS` = the page(s) to scrape
- `country` = 'CH', 'DE', or 'UK'
- The `scrape()` method — find the right CSS selectors using browser DevTools (F12)

Then add it to `SCRAPER_REGISTRY` at the bottom of the file.

---

## Settings (in the app)

All settings are changeable from the ⚙️ button in the app:

| Setting | Default |
|---------|---------|
| Actor name | Noam |
| Actor age | 14 |
| Languages | German, Swiss German, English, Japanese |
| Countries | Switzerland (CH) |
| Scan time | 08:00 Zurich time |
| Germany | E-casting only |
| UK | E-casting only |

---

## Status workflow

| Status | Meaning |
|--------|---------|
| **New** | Just found — shows on dashboard |
| **Applied** | You applied — keeps showing until archived |
| **Not Interested** | Dismissed — never shows again |
| **Archived** | Hidden from main view, viewable in archive tab |
