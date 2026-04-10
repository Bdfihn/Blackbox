"""
scheduler.py — Runs etl.py every night at 02:00 local time.
Keeps the container alive and logs next run time.
"""

import logging
import time
from datetime import datetime

import schedule
from etl import run_etl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def job():
    log.info("Scheduled ETL job starting...")
    try:
        run_etl()
    except Exception as e:
        log.error(f"ETL job failed: {e}", exc_info=True)


# Run every night at 02:00
schedule.every().day.at("02:00").do(job)

log.info("Scheduler started. ETL will run nightly at 02:00.")
log.info(f"Next run: {schedule.next_run()}")

# Also run immediately on startup so you get data right away
log.info("Running initial ETL on startup...")
job()

while True:
    schedule.run_pending()
    time.sleep(60)
