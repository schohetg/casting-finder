"""
app.py - Casting Finder Flask application
"""
import os
import json
import logging
import threading
from datetime import datetime

from flask import Flask, jsonify, request, render_template, abort

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


def log_progress(msg: str):
    """Append a timestamped line to the in-memory scan log."""
    with _scan_progress_lock:
        ts = datetime.now().strftime('%H:%M:%S')
        _scan_progress.append(f"[{ts}] {msg}")
        if len(_scan_progress) > 200:   # keep last 200 lines
            _scan_progress.pop(0)


def start_scheduler():
    global scheduler
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


def run_scan_job():
    """The actual scan job (thread-safe)."""
    global _scan_in_progress, _scan_progress
    with _scan_lock:
        if _scan_in_progress:
            logger.info("Scan already in progress, skipping.")
            return
        _scan_in_progress = True

    # Clear the log for the new scan
    with _scan_progress_lock:
        _scan_progress.clear()

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
        with _scan_lock:
            _scan_in_progress = False


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


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
    """Return in-memory scan log lines."""
    with _scan_progress_lock:
        return jsonify({
            'in_progress': _scan_in_progress,
            'logs': list(_scan_progress),
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
