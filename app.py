"""
app.py - Casting Finder Flask application
"""
import os
import json
import logging
import threading
from datetime import datetime

from flask import Flask, jsonify, request, render_template_string, abort

import database as db
from scraper import run_scrapers, SCRAPER_REGISTRY

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

# ── Scheduler ─────────────────────────────────────────────────────────────────

scheduler = None
_scan_lock = threading.Lock()
_scan_in_progress = False
_scan_progress = []          # in-memory log lines for the current/last scan
_scan_progress_lock = threading.Lock()

# Log file sits on Render's persistent disk next to the DB
_DB_DIR = os.path.dirname(os.environ.get('DB_PATH', 'castings.db')) or '.'
_LOG_FILE = os.path.join(_DB_DIR, 'scan.log')


def log_progress(msg: str):
    """Append a timestamped line to in-memory list AND the persistent log file."""
    ts = datetime.now().strftime('%H:%M:%S')
    line = f"[{ts}] {msg}"
    with _scan_progress_lock:
        _scan_progress.append(line)
        if len(_scan_progress) > 300:
            _scan_progress.pop(0)
    # Write to file immediately so logs survive any crash/restart
    try:
        with open(_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def start_scheduler():
    global scheduler
    if scheduler is not None and scheduler.running:
        logger.info("Scheduler already running — skipping duplicate start")
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        settings = db.get_settings()
        scan_hour = int(settings.get('scan_hour', 8))
        scan_minute = int(settings.get('scan_minute', 0))

        scheduler = BackgroundScheduler(timezone='Europe/Zurich')
        scheduler.add_job(
            run_scan_job,
            CronTrigger(hour=scan_hour, minute=scan_minute),
            id='daily_scan',
            replace_existing=True,
        )
        scheduler.start()
        logger.info(f"Scheduler started: daily scan at {scan_hour:02d}:{scan_minute:02d} Zurich time")
    except Exception as e:
        logger.warning(f"Could not start scheduler: {e}")


def update_scheduler_time(hour: int, minute: int):
    """Update the daily scan time without restarting the whole scheduler."""
    global scheduler
    if scheduler is None:
        return
    try:
        from apscheduler.triggers.cron import CronTrigger
        scheduler.reschedule_job(
            'daily_scan',
            trigger=CronTrigger(hour=hour, minute=minute),
        )
        logger.info(f"Scheduler updated: daily scan at {hour:02d}:{minute:02d}")
    except Exception as e:
        logger.warning(f"Could not update scheduler: {e}")


class _ProgressCapture(logging.Handler):
    """Routes scraper logger.info() lines into the live progress panel."""
    def emit(self, record):
        try:
            log_progress(f"      ↳ {record.getMessage()}")
        except Exception:
            pass


def run_scan_job():
    """The actual scan job (thread-safe)."""
    global _scan_in_progress, _scan_progress
    with _scan_lock:
        if _scan_in_progress:
            logger.info("Scan already in progress, skipping.")
            return
        _scan_in_progress = True

    # Clear the log for the new scan (memory + file)
    with _scan_progress_lock:
        _scan_progress.clear()
    try:
        with open(_LOG_FILE, 'w', encoding='utf-8') as f:
            f.write('')   # truncate
    except Exception:
        pass

    # Capture scraper-level INFO logs into the progress panel
    _capture = _ProgressCapture(level=logging.INFO)
    _scraper_logger = logging.getLogger('scraper')
    _scraper_logger.addHandler(_capture)

    try:
        log_progress("🔍 Scan started…")
        settings = db.get_settings()
        log_id = db.start_scan_log()

        enabled_sites = settings.get('enabled_sites', [])
        enabled_countries = settings.get('enabled_countries', ['CH'])
        log_progress(f"🌍 Countries: {', '.join(enabled_countries)}")
        log_progress(f"🌐 Sites: {', '.join(enabled_sites)}")

        castings_all = []
        total_stats = {'scraped': 0, 'relevant': 0, 'filtered': 0, 'errors': []}

        for site_name in enabled_sites:
            scraper_cls = SCRAPER_REGISTRY.get(site_name)
            if not scraper_cls:
                continue
            scraper = scraper_cls(settings)
            if scraper.country not in enabled_countries:
                log_progress(f"⏭️  {site_name} — skipped (country {scraper.country} not enabled)")
                continue

            log_progress(f"🔎 Scraping {site_name}…")
            try:
                castings = scraper.scrape()
                relevant = []
                filtered = 0
                for c in castings:
                    ok, reason = scraper.is_relevant(c)
                    if ok:
                        relevant.append(c)
                    else:
                        filtered += 1
                log_progress(f"   ✅ {site_name}: {len(relevant)} relevant, {filtered} filtered out")
                castings_all.extend(relevant)
                total_stats['scraped'] += len(castings)
                total_stats['relevant'] += len(relevant)
                total_stats['filtered'] += filtered
            except Exception as e:
                msg = f"{site_name}: {e}"
                log_progress(f"   ❌ {site_name}: error — {e}")
                total_stats['errors'].append(msg)

        # Deduplicate and save
        seen = set()
        new_count = 0
        for casting in castings_all:
            cid = casting.get('id')
            if cid in seen:
                continue
            seen.add(cid)
            is_new = db.upsert_casting(casting)
            if is_new:
                new_count += 1

        db.finish_scan_log(
            log_id,
            found=total_stats['relevant'],
            new_count=new_count,
            filtered=total_stats['filtered'],
            errors=total_stats['errors'],
        )

        log_progress(f"🎉 Done! {new_count} new castings added, {total_stats['filtered']} filtered out.")
        if total_stats['errors']:
            log_progress(f"⚠️  {len(total_stats['errors'])} site(s) had errors.")
        logger.info(f"Scan done: {new_count} new castings.")

    except Exception as e:
        log_progress(f"💥 Scan failed: {e}")
        logger.error(f"Scan job failed: {e}")
    finally:
        # Remove the progress capture handler
        try:
            _scraper_logger.removeHandler(_capture)
        except Exception:
            pass
        with _scan_lock:
            _scan_in_progress = False


# ── Routes ────────────────────────────────────────────────────────────────────

# ── Embedded dashboard HTML ───────────────────────────────────────────────────

_DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>🎬 Casting Finder – Noam</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/alpinejs/3.13.5/cdn.min.js" defer></script>
  <style>
    :root {
      --bg: #0f1117;
      --surface: #1a1d27;
      --card: #21253a;
      --border: #2e3250;
      --accent: #6c63ff;
      --accent2: #ff6584;
      --green: #22c55e;
      --yellow: #f59e0b;
      --red: #ef4444;
      --blue: #3b82f6;
      --text: #e2e8f0;
      --muted: #94a3b8;
      --radius: 12px;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
    }

    /* ─── Header ─────────────────────────────────────────── */
    header {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 0 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      height: 64px;
      position: sticky;
      top: 0;
      z-index: 100;
    }

    .logo {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 1.2rem;
      font-weight: 700;
      color: var(--text);
    }

    .logo span { color: var(--accent); }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    /* ─── Buttons ─────────────────────────────────────────── */
    .btn {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 8px 16px;
      border-radius: 8px;
      border: none;
      font-size: 0.875rem;
      font-weight: 500;
      cursor: pointer;
      transition: opacity 0.15s, transform 0.1s;
    }
    .btn:hover { opacity: 0.85; }
    .btn:active { transform: scale(0.97); }
    .btn:disabled { opacity: 0.4; cursor: not-allowed; }

    .btn-primary { background: var(--accent); color: #fff; }
    .btn-ghost { background: var(--card); color: var(--text); border: 1px solid var(--border); }
    .btn-danger { background: rgba(239,68,68,0.15); color: var(--red); border: 1px solid rgba(239,68,68,0.3); }
    .btn-success { background: rgba(34,197,94,0.15); color: var(--green); border: 1px solid rgba(34,197,94,0.3); }
    .btn-warning { background: rgba(245,158,11,0.15); color: var(--yellow); border: 1px solid rgba(245,158,11,0.3); }
    .btn-sm { padding: 5px 10px; font-size: 0.8rem; }
    .btn-icon { padding: 8px; }

    /* ─── Stats bar ───────────────────────────────────────── */
    .stats-bar {
      display: flex;
      gap: 16px;
      padding: 16px 24px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      overflow-x: auto;
    }

    .stat-pill {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 16px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 50px;
      white-space: nowrap;
      cursor: pointer;
      transition: border-color 0.15s;
    }
    .stat-pill:hover { border-color: var(--accent); }
    .stat-pill.active { border-color: var(--accent); background: rgba(108,99,255,0.15); }
    .stat-pill .count {
      font-size: 1.1rem;
      font-weight: 700;
    }
    .stat-pill .label { font-size: 0.8rem; color: var(--muted); }
    .pill-new .count { color: var(--accent); }
    .pill-applied .count { color: var(--green); }
    .pill-archived .count { color: var(--muted); }

    .scan-info {
      margin-left: auto;
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 0.8rem;
      color: var(--muted);
      white-space: nowrap;
    }

    /* ─── Filter bar ──────────────────────────────────────── */
    .filter-bar {
      padding: 12px 24px;
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }

    .filter-label { font-size: 0.8rem; color: var(--muted); }

    .filter-group { display: flex; gap: 6px; }

    .filter-btn {
      padding: 5px 12px;
      border-radius: 6px;
      font-size: 0.8rem;
      cursor: pointer;
      border: 1px solid var(--border);
      background: transparent;
      color: var(--muted);
      transition: all 0.15s;
    }
    .filter-btn:hover { color: var(--text); border-color: var(--accent); }
    .filter-btn.active {
      background: rgba(108,99,255,0.2);
      border-color: var(--accent);
      color: var(--text);
    }

    /* ─── Grid ────────────────────────────────────────────── */
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
      gap: 16px;
      padding: 0 24px 24px;
    }

    /* ─── Casting card ────────────────────────────────────── */
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 18px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      transition: border-color 0.15s, transform 0.1s;
      cursor: pointer;
    }
    .card:hover {
      border-color: var(--accent);
      transform: translateY(-2px);
    }
    .card.applied { border-left: 3px solid var(--green); }

    .card-top {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 8px;
    }

    .card-title {
      font-size: 0.95rem;
      font-weight: 600;
      line-height: 1.4;
      flex: 1;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 0.72rem;
      font-weight: 600;
      white-space: nowrap;
    }
    .badge-site { background: rgba(108,99,255,0.2); color: #a5b4fc; }
    .badge-age  { background: rgba(34,197,94,0.15); color: var(--green); }
    .badge-age.unknown { background: rgba(148,163,184,0.1); color: var(--muted); }
    .badge-male   { background: rgba(59,130,246,0.15); color: #93c5fd; }
    .badge-female { background: rgba(255,101,132,0.15); color: #fca5a5; }
    .badge-any    { background: rgba(148,163,184,0.1); color: var(--muted); }
    .badge-ch  { background: rgba(239,68,68,0.1); color: #fca5a5; }
    .badge-de  { background: rgba(245,158,11,0.1); color: #fcd34d; }
    .badge-uk  { background: rgba(59,130,246,0.1); color: #93c5fd; }

    .badge-deadline-ok      { background: rgba(34,197,94,0.1);  color: var(--green); }
    .badge-deadline-soon    { background: rgba(245,158,11,0.2); color: var(--yellow); }
    .badge-deadline-urgent  { background: rgba(239,68,68,0.2);  color: var(--red); }
    .badge-deadline-unknown { background: rgba(245,158,11,0.15); color: var(--yellow); border: 1px solid rgba(245,158,11,0.4); }
    .badge-new    { background: rgba(108,99,255,0.2); color: #a5b4fc; }
    .badge-applied { background: rgba(34,197,94,0.2); color: var(--green); }
    .badge-pdf { background: rgba(245,158,11,0.15); color: var(--yellow); }

    .card-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      align-items: center;
    }

    .card-desc {
      font-size: 0.82rem;
      color: var(--muted);
      line-height: 1.5;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    .card-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 4px;
      padding-top: 12px;
      border-top: 1px solid var(--border);
    }

    /* ─── Empty state ─────────────────────────────────────── */
    .empty {
      grid-column: 1 / -1;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 64px 24px;
      text-align: center;
      color: var(--muted);
      gap: 12px;
    }
    .empty-icon { font-size: 4rem; }

    /* ─── Modal ───────────────────────────────────────────── */
    .overlay {
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.7);
      z-index: 200;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }

    .modal {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      max-width: 680px;
      width: 100%;
      max-height: 85vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }

    .modal-header {
      padding: 20px 24px 16px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: flex-start;
      gap: 12px;
    }

    .modal-title {
      flex: 1;
      font-size: 1.05rem;
      font-weight: 700;
      line-height: 1.4;
    }

    .modal-body {
      padding: 20px 24px;
      overflow-y: auto;
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    .modal-footer {
      padding: 16px 24px;
      border-top: 1px solid var(--border);
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }

    .detail-section h4 {
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
      margin-bottom: 8px;
    }

    .detail-text {
      font-size: 0.87rem;
      line-height: 1.6;
      color: var(--text);
      white-space: pre-wrap;
    }

    .pdf-box {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 16px;
      font-size: 0.82rem;
      line-height: 1.6;
      color: var(--muted);
      white-space: pre-wrap;
      max-height: 200px;
      overflow-y: auto;
    }

    /* ─── Settings modal ──────────────────────────────────── */
    .settings-modal {
      max-width: 560px;
    }

    .form-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .form-group label {
      font-size: 0.82rem;
      font-weight: 600;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .form-input, .form-select {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      color: var(--text);
      font-size: 0.9rem;
      outline: none;
      transition: border-color 0.15s;
      width: 100%;
    }
    .form-input:focus, .form-select:focus { border-color: var(--accent); }

    .checkbox-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
      gap: 8px;
    }

    .checkbox-item {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      cursor: pointer;
      font-size: 0.85rem;
      transition: border-color 0.15s;
      user-select: none;
    }
    .checkbox-item:hover { border-color: var(--accent); }
    .checkbox-item input[type=checkbox] { accent-color: var(--accent); }

    .settings-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }

    /* ─── Spinner ─────────────────────────────────────────── */
    @keyframes spin { to { transform: rotate(360deg); } }
    .spinner {
      width: 16px;
      height: 16px;
      border: 2px solid rgba(255,255,255,0.3);
      border-top-color: #fff;
      border-radius: 50%;
      animation: spin 0.7s linear infinite;
    }

    /* ─── Toast ───────────────────────────────────────────── */
    .toast-area {
      position: fixed;
      bottom: 24px;
      right: 24px;
      z-index: 999;
      display: flex;
      flex-direction: column;
      gap: 8px;
      pointer-events: none;
    }
    .toast {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 16px;
      font-size: 0.85rem;
      animation: slideIn 0.2s ease;
      pointer-events: all;
    }
    @keyframes slideIn { from { transform: translateX(120%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

    /* ─── Scrollbar ───────────────────────────────────────── */
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

    /* ─── Scan pulse ──────────────────────────────────────── */
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
    .scan-pulse { animation: pulse 1.5s ease-in-out infinite; color: var(--yellow); }
  </style>
</head>
<body x-data="app()" x-init="init()">

  <!-- ─── Header ─────────────────────────────────────────── -->
  <header>
    <div class="logo">
      🎬 <span>Casting</span>Finder
      <small style="font-size:0.7rem;color:var(--muted);font-weight:400;margin-left:4px">for Noam</small>
    </div>
    <div class="header-actions">
      <span x-show="scanInProgress" class="scan-pulse" style="font-size:0.82rem">⏳ Scanning…</span>
      <button class="btn btn-ghost" @click="openSettings()">⚙️ Settings</button>
      <button class="btn btn-primary" @click="triggerScan()" :disabled="scanInProgress">
        <span x-show="!scanInProgress">🔍 Scan Now</span>
        <span x-show="scanInProgress" style="display:flex;align-items:center;gap:6px"><div class="spinner"></div>Scanning</span>
      </button>
    </div>
  </header>

  <!-- ─── Stats bar ──────────────────────────────────────── -->
  <div class="stats-bar">
    <div class="stat-pill pill-new" :class="{active: statusFilter==='new'}" @click="statusFilter='new'; loadCastings()">
      <span class="count" x-text="stats.new ?? 0"></span>
      <span class="label">New</span>
    </div>
    <div class="stat-pill pill-applied" :class="{active: statusFilter==='applied'}" @click="statusFilter='applied'; loadCastings()">
      <span class="count" x-text="stats.applied ?? 0"></span>
      <span class="label">Applied</span>
    </div>
    <div class="stat-pill" :class="{active: statusFilter==='all'}" @click="statusFilter='all'; loadCastings()">
      <span class="count" style="color:var(--muted)" x-text="(stats.new??0)+(stats.applied??0)"></span>
      <span class="label">All active</span>
    </div>
    <div class="stat-pill pill-archived" :class="{active: statusFilter==='archived'}" @click="statusFilter='archived'; loadCastings()">
      <span class="count" x-text="stats.archived ?? 0"></span>
      <span class="label">Archived</span>
    </div>

    <div class="scan-info">
      <span x-show="lastScan">Last scan: <strong x-text="lastScanText"></strong></span>
      <span x-show="!lastScan">No scan yet</span>
    </div>
  </div>

  <!-- ─── Filter bar ─────────────────────────────────────── -->
  <div class="filter-bar">
    <span class="filter-label">Country:</span>
    <div class="filter-group">
      <button class="filter-btn" :class="{active: countryFilter===''}" @click="countryFilter=''; loadCastings()">All</button>
      <template x-for="c in enabledCountries" :key="c">
        <button class="filter-btn" :class="{active: countryFilter===c}" @click="countryFilter=c; loadCastings()" x-text="c"></button>
      </template>
    </div>
    <div style="margin-left:auto;display:flex;align-items:center;gap:8px">
      <span class="filter-label" x-text="castings.length + ' castings'"></span>
    </div>
  </div>

  <!-- ─── Scan Log Panel ────────────────────────────────── -->
  <div style="padding: 0 24px 16px">
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden">
      <div
        style="padding:10px 16px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;user-select:none"
        @click="scanLogsOpen = !scanLogsOpen"
      >
        <span style="font-size:0.85rem;font-weight:600;display:flex;align-items:center;gap:8px">
          <span x-show="scanInProgress" class="scan-pulse">⏳</span>
          <span x-show="!scanInProgress">📋</span>
          <span x-show="scanInProgress">Scanning in progress…</span>
          <span x-show="!scanInProgress" x-text="scanLogs.length > 0 ? 'Last scan log' : 'Scan log (no scans yet)'"></span>
        </span>
        <span style="color:var(--muted);font-size:0.8rem" x-text="scanLogsOpen ? '▲ Hide' : '▼ Show'"></span>
      </div>
      <div x-show="scanLogsOpen" style="border-top:1px solid var(--border);padding:12px 16px;max-height:300px;overflow-y:auto;font-family:monospace;font-size:0.78rem;line-height:1.8;color:var(--muted)" x-ref="logPanel">
        <template x-for="(line, i) in scanLogs" :key="i">
          <div x-text="line" :style="line.includes('❌') || line.includes('💥') ? 'color:var(--red)' : line.includes('🎉') ? 'color:var(--green)' : line.includes('⚠️') ? 'color:var(--yellow)' : line.includes('↳') ? 'color:#6b7280;font-size:0.72rem' : ''"></div>
        </template>
        <div x-show="scanInProgress && scanLogs.length === 0" style="color:var(--muted)">Starting…</div>
        <div x-show="!scanInProgress && scanLogs.length === 0" style="color:var(--muted)">No scans have run yet. Click <strong>Scan Now</strong> to start.</div>
      </div>
    </div>
  </div>

  <!-- ─── Castings grid ──────────────────────────────────── -->
  <div class="grid" x-show="!loading">

    <!-- Empty state -->
    <div class="empty" x-show="castings.length === 0">
      <span class="empty-icon">🎭</span>
      <h3>No castings found</h3>
      <p x-show="statusFilter==='new'">Click <strong>Scan Now</strong> to search for open castings.</p>
      <p x-show="statusFilter!=='new'">Nothing in this category yet.</p>
    </div>

    <!-- Cards -->
    <template x-for="c in castings" :key="c.id">
      <div class="card" :class="c.status" @click="openDetail(c)">

        <div class="card-top">
          <div class="card-title" x-text="c.title"></div>
          <div class="badge badge-new" x-show="c.status==='new'">New</div>
          <div class="badge badge-applied" x-show="c.status==='applied'">Applied</div>
        </div>

        <!-- Meta badges -->
        <div class="card-meta">
          <span class="badge badge-site" x-text="c.source_site"></span>
          <span class="badge" :class="'badge-'+c.country.toLowerCase()" x-text="c.country"></span>

          <!-- Age — show clearly, warn if unknown -->
          <template x-if="c.age_min !== null && c.age_max !== null">
            <span class="badge badge-age" x-text="c.age_min===c.age_max ? '👤 '+c.age_min+'y' : '👤 '+c.age_min+'–'+c.age_max+'y'"></span>
          </template>
          <template x-if="c.age_min === null">
            <span class="badge badge-age unknown" title="Age range not found in listing — check manually">👤 Age unknown</span>
          </template>

          <!-- Gender -->
          <span class="badge badge-male"   x-show="c.gender==='male'">👦 Male</span>
          <span class="badge badge-female" x-show="c.gender==='female'">👧 Female</span>
          <span class="badge badge-any"    x-show="c.gender==='any' || !c.gender">⚧ Any</span>

          <!-- Deadline -->
          <template x-if="c.deadline">
            <span class="badge" :class="deadlineBadge(c.deadline)" x-text="'📅 '+formatDeadline(c.deadline)"></span>
          </template>
          <template x-if="!c.deadline">
            <span class="badge badge-deadline-unknown" title="No deadline found — check the listing manually">⚠️ Deadline unknown</span>
          </template>

          <!-- PDF indicator -->
          <span class="badge badge-pdf" x-show="c.pdf_urls && c.pdf_urls.length > 0">📄 PDF</span>
        </div>

        <!-- Description snippet — first 250 chars, clearly truncated -->
        <div class="card-desc" x-text="c.description ? c.description.trim().slice(0, 250) + (c.description.length > 250 ? '…' : '') : '(No description extracted — click to open the casting page)'"></div>

        <!-- Source URL hint -->
        <div style="font-size:0.75rem;color:var(--muted);opacity:0.6;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" x-text="c.source_url"></div>

        <!-- Action buttons (stop propagation so card click doesn't fire) -->
        <div class="card-actions" @click.stop>
          <a :href="c.source_url" target="_blank" class="btn btn-ghost btn-sm">🔗 Open</a>

          <template x-if="c.status !== 'applied'">
            <button class="btn btn-success btn-sm" @click="setStatus(c, 'applied')">✅ Applied</button>
          </template>
          <template x-if="c.status === 'applied'">
            <button class="btn btn-ghost btn-sm" @click="setStatus(c, 'new')">↩️ Undo</button>
          </template>

          <template x-if="c.status !== 'archived'">
            <button class="btn btn-warning btn-sm" @click="setStatus(c, 'archived')">📦 Archive</button>
          </template>

          <button class="btn btn-danger btn-sm" @click="setStatus(c, 'not_interested')">❌ Not Interested</button>
        </div>
      </div>
    </template>
  </div>

  <!-- Loading state -->
  <div x-show="loading" style="display:flex;justify-content:center;padding:64px">
    <div class="spinner" style="width:32px;height:32px;border-width:3px"></div>
  </div>

  <!-- ─── Detail Modal ────────────────────────────────────── -->
  <div class="overlay" x-show="detailCasting" @click.self="detailCasting=null" x-transition>
    <div class="modal" x-show="detailCasting" @click.stop>
      <template x-if="detailCasting">
        <div style="display:flex;flex-direction:column;height:100%">
          <div class="modal-header">
            <div style="flex:1">
              <div class="modal-title" x-text="detailCasting.title"></div>
              <div class="card-meta" style="margin-top:8px">
                <span class="badge badge-site" x-text="detailCasting.source_site"></span>
                <span class="badge" :class="'badge-'+detailCasting.country.toLowerCase()" x-text="detailCasting.country"></span>
                <template x-if="detailCasting.age_min !== null">
                  <span class="badge badge-age" x-text="detailCasting.age_min===detailCasting.age_max ? detailCasting.age_min+'y' : detailCasting.age_min+'-'+detailCasting.age_max+'y'"></span>
                </template>
                <span class="badge badge-male"   x-show="detailCasting.gender==='male'">👦 Male</span>
                <span class="badge badge-female" x-show="detailCasting.gender==='female'">👧 Female</span>
                <span class="badge badge-any"    x-show="detailCasting.gender==='any' || !detailCasting.gender">Any gender</span>
                <template x-if="detailCasting.deadline">
                  <span class="badge" :class="deadlineBadge(detailCasting.deadline)" x-text="'📅 Deadline: '+detailCasting.deadline"></span>
                </template>
                <template x-if="!detailCasting.deadline">
                  <span class="badge badge-deadline-unknown">⚠️ Deadline unknown — check listing</span>
                </template>
              </div>
            </div>
            <button class="btn btn-ghost btn-icon" @click="detailCasting=null">✕</button>
          </div>

          <div class="modal-body">
            <div class="detail-section">
              <h4>Description</h4>
              <div class="detail-text" x-text="detailCasting.description || 'No description available.'"></div>
            </div>

            <template x-if="detailCasting.pdf_content">
              <div class="detail-section">
                <h4>📄 PDF Content</h4>
                <div class="pdf-box" x-text="detailCasting.pdf_content"></div>
              </div>
            </template>

            <div class="detail-section">
              <h4>Found</h4>
              <div style="font-size:0.85rem;color:var(--muted)" x-text="detailCasting.found_date ? new Date(detailCasting.found_date).toLocaleDateString('en-GB') : 'Unknown'"></div>
            </div>
          </div>

          <div class="modal-footer">
            <a :href="detailCasting.source_url" target="_blank" class="btn btn-primary">🔗 Open Casting Page</a>
            <template x-if="detailCasting.status !== 'applied'">
              <button class="btn btn-success" @click="setStatus(detailCasting, 'applied'); detailCasting=null">✅ Mark as Applied</button>
            </template>
            <template x-if="detailCasting.status === 'applied'">
              <button class="btn btn-ghost" @click="setStatus(detailCasting, 'new'); detailCasting=null">↩️ Mark as New</button>
            </template>
            <button class="btn btn-warning" @click="setStatus(detailCasting, 'archived'); detailCasting=null">📦 Archive</button>
            <button class="btn btn-danger" @click="setStatus(detailCasting, 'not_interested'); detailCasting=null">❌ Not Interested</button>
          </div>
        </div>
      </template>
    </div>
  </div>

  <!-- ─── Settings Modal ──────────────────────────────────── -->
  <div class="overlay" x-show="settingsOpen" @click.self="settingsOpen=false" x-transition>
    <div class="modal settings-modal" x-show="settingsOpen" @click.stop>
      <div class="modal-header">
        <div class="modal-title">⚙️ Settings</div>
        <button class="btn btn-ghost btn-icon" @click="settingsOpen=false">✕</button>
      </div>

      <div class="modal-body">
        <!-- Actor info -->
        <div class="settings-grid">
          <div class="form-group">
            <label>Actor Name</label>
            <input class="form-input" type="text" x-model="settings.actor_name" />
          </div>
          <div class="form-group">
            <label>Actor Age</label>
            <input class="form-input" type="number" min="1" max="99" x-model.number="settings.actor_age" />
          </div>
        </div>

        <!-- Languages -->
        <div class="form-group">
          <label>Languages (actor speaks)</label>
          <div class="checkbox-grid">
            <template x-for="lang in allLanguages" :key="lang">
              <label class="checkbox-item">
                <input type="checkbox" :value="lang" x-model="settings.actor_languages" />
                <span x-text="lang"></span>
              </label>
            </template>
          </div>
        </div>

        <!-- Countries -->
        <div class="form-group">
          <label>Countries to search</label>
          <div class="checkbox-grid">
            <label class="checkbox-item">
              <input type="checkbox" value="CH" x-model="settings.enabled_countries" />
              🇨🇭 Switzerland
            </label>
            <label class="checkbox-item">
              <input type="checkbox" value="DE" x-model="settings.enabled_countries" />
              🇩🇪 Germany
            </label>
            <label class="checkbox-item">
              <input type="checkbox" value="UK" x-model="settings.enabled_countries" />
              🇬🇧 United Kingdom
            </label>
          </div>
        </div>

        <!-- E-casting options -->
        <div class="form-group">
          <label>Country restrictions</label>
          <label class="checkbox-item">
            <input type="checkbox" x-model="settings.de_ecast_only" :true-value="'true'" :false-value="'false'" />
            Germany: only e-casting / self-tape allowed (open to non-residents)
          </label>
          <label class="checkbox-item" style="margin-top:6px">
            <input type="checkbox" x-model="settings.uk_ecast_only" :true-value="'true'" :false-value="'false'" />
            UK: only e-casting / self-tape allowed (open to non-residents)
          </label>
        </div>

        <!-- Sites -->
        <div class="form-group">
          <label>Casting sites enabled</label>
          <div class="checkbox-grid">
            <template x-for="site in allSites" :key="site">
              <label class="checkbox-item">
                <input type="checkbox" :value="site" x-model="settings.enabled_sites" />
                <span x-text="site"></span>
              </label>
            </template>
          </div>
        </div>

        <!-- Scan schedule -->
        <div class="form-group">
          <label>Daily scan time (Zurich / CET)</label>
          <div style="display:flex;gap:10px;align-items:center">
            <input class="form-input" type="number" min="0" max="23" x-model.number="settings.scan_hour" style="width:80px" />
            <span style="color:var(--muted)">:</span>
            <input class="form-input" type="number" min="0" max="59" x-model.number="settings.scan_minute" style="width:80px" />
            <span style="color:var(--muted);font-size:0.82rem">(HH : MM)</span>
          </div>
        </div>
      </div>

      <div class="modal-footer">
        <button class="btn btn-primary" @click="saveSettings()">💾 Save Settings</button>
        <button class="btn btn-ghost" @click="settingsOpen=false">Cancel</button>
      </div>
    </div>
  </div>

  <!-- ─── Toast area ──────────────────────────────────────── -->
  <div class="toast-area">
    <template x-for="toast in toasts" :key="toast.id">
      <div class="toast" x-text="toast.msg"></div>
    </template>
  </div>

  <!-- ─── Alpine.js App ────────────────────────────────────── -->
  <script>
    function app() {
      return {
        castings: [],
        stats: {},
        loading: false,
        statusFilter: 'new',
        countryFilter: '',
        scanInProgress: false,
        lastScan: null,
        detailCasting: null,
        settingsOpen: false,
        // Pre-initialize arrays so Alpine knows they are arrays before API loads
        settings: {
          actor_name: 'Noam',
          actor_age: 14,
          actor_languages: [],
          enabled_countries: [],
          enabled_sites: [],
          de_ecast_only: 'true',
          uk_ecast_only: 'true',
          scan_hour: 8,
          scan_minute: 0,
        },
        allLanguages: ['German', 'Swiss German', 'English', 'Japanese', 'French', 'Italian'],
        allSites: [],
        enabledCountries: ['CH'],
        toasts: [],
        scanLogs: [],
        scanLogsOpen: true,   // always open by default
        _wasScanRunning: false,

        // ─── Lifecycle ────────────────────────────────────────────

        async init() {
          await Promise.all([this.loadCastings(), this.loadStats(), this.loadScanStatus(), this.loadSettings(), this.loadScanLogs()]);
          this.pollScan();
        },

        // ─── Data loading ─────────────────────────────────────────

        async loadCastings() {
          this.loading = true;
          const params = new URLSearchParams();
          if (this.statusFilter && this.statusFilter !== 'all') params.set('status', this.statusFilter);
          if (this.countryFilter) params.set('country', this.countryFilter);

          try {
            const r = await fetch('/api/castings?' + params);
            const data = await r.json();
            this.castings = data.castings;
          } catch(e) {
            this.toast('Failed to load castings');
          }
          this.loading = false;
        },

        async loadStats() {
          try {
            const r = await fetch('/api/stats');
            this.stats = await r.json();
          } catch(e) {}
        },

        async loadScanStatus() {
          try {
            const r = await fetch('/api/scan/status');
            const d = await r.json();
            this.scanInProgress = d.in_progress;
            this.lastScan = d.last_scan;
          } catch(e) {}
        },

        async loadSettings() {
          try {
            const r = await fetch('/api/settings');
            const d = await r.json();
            // Ensure array fields are real arrays (never strings)
            const ensureArray = (v) => Array.isArray(v) ? v : (typeof v === 'string' ? JSON.parse(v) : []);
            d.actor_languages   = ensureArray(d.actor_languages);
            d.enabled_countries = ensureArray(d.enabled_countries);
            d.enabled_sites     = ensureArray(d.enabled_sites);
            this.settings = d;
            this.allSites = d.available_sites || [];
            this.enabledCountries = d.enabled_countries || ['CH'];
          } catch(e) { console.error('loadSettings error', e); }
        },

        // ─── Actions ──────────────────────────────────────────────

        async triggerScan() {
          if (this.scanInProgress) return;
          this.scanInProgress = true;
          this.scanLogs = [];
          this.scanLogsOpen = true;   // open log panel immediately
          try {
            const r = await fetch('/api/scan', {method: 'POST'});
            const d = await r.json();
            if (d.ok) {
              this.toast('🔍 Scan started…');
            } else {
              this.toast(d.message || 'Scan already running');
              this.scanInProgress = false;
            }
          } catch(e) {
            this.toast('Failed to start scan');
            this.scanInProgress = false;
          }
        },

        async setStatus(casting, newStatus) {
          try {
            const r = await fetch(`/api/castings/${casting.id}/status`, {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({status: newStatus}),
            });
            if (r.ok) {
              casting.status = newStatus;
              // Remove from list if it no longer belongs in current view
              if (newStatus === 'not_interested' || newStatus === 'archived') {
                this.castings = this.castings.filter(c => c.id !== casting.id);
              }
              await this.loadStats();

              const msgs = {
                applied: '✅ Marked as applied!',
                not_interested: '❌ Dismissed — won\'t show again.',
                archived: '📦 Archived.',
                new: '↩️ Reset to new.',
              };
              this.toast(msgs[newStatus] || 'Updated');
            }
          } catch(e) {
            this.toast('Failed to update status');
          }
        },

        openDetail(casting) {
          this.detailCasting = casting;
        },

        openSettings() {
          this.settingsOpen = true;
        },

        async saveSettings() {
          try {
            const r = await fetch('/api/settings', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify(this.settings),
            });
            if (r.ok) {
              this.enabledCountries = this.settings.enabled_countries || ['CH'];
              this.settingsOpen = false;
              this.toast('✅ Settings saved!');
              await this.loadCastings();
            }
          } catch(e) {
            this.toast('Failed to save settings');
          }
        },

        // ─── Polling ──────────────────────────────────────────────

        pollScan() {
          setInterval(async () => {
            await this.loadScanStatus();
            // Always load logs while scanning or right after scan finishes
            if (this.scanInProgress || this._wasScanRunning) {
              await this.loadScanLogs();
              // Auto-scroll log panel to bottom
              this.$nextTick(() => {
                const el = this.$refs.logPanel;
                if (el) el.scrollTop = el.scrollHeight;
              });
            }
            // Refresh castings when scan finishes
            if (!this.scanInProgress && this._wasScanRunning) {
              await this.loadScanLogs();  // one final load to get complete log
              await this.loadCastings();
              await this.loadStats();
              this.toast('✅ Scan complete! Castings updated.');
            }
            this._wasScanRunning = this.scanInProgress;
          }, 2000);
        },

        async loadScanLogs() {
          try {
            const r = await fetch('/api/scan/progress');
            const d = await r.json();
            this.scanLogs = d.logs || [];
          } catch(e) {}
        },

        // ─── Helpers ──────────────────────────────────────────────

        formatDeadline(deadline) {
          if (!deadline) return '';
          const d = new Date(deadline);
          return d.toLocaleDateString('en-GB', {day:'numeric', month:'short'});
        },

        deadlineBadge(deadline) {
          if (!deadline) return 'badge-deadline-ok';
          const days = Math.ceil((new Date(deadline) - new Date()) / 86400000);
          if (days <= 3) return 'badge-deadline-urgent';
          if (days <= 7) return 'badge-deadline-soon';
          return 'badge-deadline-ok';
        },

        get lastScanText() {
          if (!this.lastScan) return '';
          const finished = this.lastScan.finished_at;
          if (!finished) return 'in progress';
          const d = new Date(finished + 'Z');
          const now = new Date();
          const diffMin = Math.round((now - d) / 60000);
          if (diffMin < 1) return 'just now';
          if (diffMin < 60) return `${diffMin}m ago`;
          const diffH = Math.round(diffMin / 60);
          if (diffH < 24) return `${diffH}h ago`;
          return d.toLocaleDateString('en-GB');
        },

        toast(msg) {
          const id = Date.now();
          this.toasts.push({id, msg});
          setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, 3500);
        },
      };
    }
  </script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(_DASHBOARD_HTML)


# ── API: Castings ─────────────────────────────────────────────────────────────

@app.route('/api/castings', methods=['GET'])
def api_castings():
    status = request.args.get('status')          # new | applied | archived | all
    country = request.args.get('country')         # CH | DE | UK
    settings = db.get_settings()
    actor_age = int(settings.get('actor_age', 14))

    castings = db.get_castings(
        status_filter=status,
        country_filter=country,
        actor_age=actor_age,
    )
    return jsonify({'castings': castings, 'count': len(castings)})


@app.route('/api/castings/<casting_id>', methods=['GET'])
def api_casting_detail(casting_id):
    casting = db.get_casting(casting_id)
    if not casting:
        abort(404)
    return jsonify(casting)


@app.route('/api/castings/<casting_id>/status', methods=['POST'])
def api_update_status(casting_id):
    data = request.get_json(force=True)
    new_status = data.get('status')
    if not new_status:
        return jsonify({'error': 'status required'}), 400
    try:
        db.update_casting_status(casting_id, new_status)
        return jsonify({'ok': True, 'id': casting_id, 'status': new_status})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


# ── API: Settings ─────────────────────────────────────────────────────────────

@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    settings = db.get_settings()
    settings['available_sites'] = list(SCRAPER_REGISTRY.keys())
    return jsonify(settings)


@app.route('/api/settings', methods=['POST'])
def api_update_settings():
    data = request.get_json(force=True)
    db.update_settings(data)

    # Update scheduler if time changed
    if 'scan_hour' in data or 'scan_minute' in data:
        settings = db.get_settings()
        update_scheduler_time(
            int(settings.get('scan_hour', 8)),
            int(settings.get('scan_minute', 0)),
        )

    return jsonify({'ok': True})


# ── API: Scan ─────────────────────────────────────────────────────────────────

@app.route('/api/scan', methods=['POST'])
def api_trigger_scan():
    """Trigger an ad-hoc scan in the background."""
    global _scan_in_progress
    with _scan_lock:
        if _scan_in_progress:
            return jsonify({'ok': False, 'message': 'Scan already in progress'}), 409

    thread = threading.Thread(target=run_scan_job, daemon=True)
    thread.start()
    return jsonify({'ok': True, 'message': 'Scan started'})


@app.route('/api/scan/status', methods=['GET'])
def api_scan_status():
    global _scan_in_progress
    last = db.get_last_scan()
    return jsonify({
        'in_progress': _scan_in_progress,
        'last_scan': last,
    })


@app.route('/api/scan/progress', methods=['GET'])
def api_scan_progress():
    """Return scan log lines — in-memory, or from persistent log file after restart."""
    with _scan_progress_lock:
        logs = list(_scan_progress)
    # If nothing in memory (server restarted), read from the log file on disk
    if not logs:
        try:
            if os.path.exists(_LOG_FILE):
                with open(_LOG_FILE, 'r', encoding='utf-8') as f:
                    logs = [l.rstrip() for l in f.readlines() if l.strip()]
                logs = logs[-300:]   # cap at 300 lines
        except Exception:
            pass
    return jsonify({
        'in_progress': _scan_in_progress,
        'logs': logs,
    })


# ── API: Stats ────────────────────────────────────────────────────────────────

@app.route('/api/stats', methods=['GET'])
def api_stats():
    return jsonify(db.get_stats())


# ── Startup ───────────────────────────────────────────────────────────────────

# ── Initialize on import (runs for both gunicorn and direct python) ───────────

db.init_db()
start_scheduler()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
