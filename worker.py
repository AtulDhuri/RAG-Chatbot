"""Run the durable crawl/embedding worker: python worker.py"""
import logging
from app import indexer
from app import jobs

logging.basicConfig(level=logging.INFO)

while True:
    job = jobs.next_job()
    if not job:
        continue
    domain = job["domain"]
    try:
        indexer.reindex(domain)
    finally:
        jobs.release(domain)
