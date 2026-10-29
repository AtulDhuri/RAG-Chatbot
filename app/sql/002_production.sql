-- Production persistence for vector versions, crawl jobs, and aggregated abuse events.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS crawl_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  domain TEXT NOT NULL,
  job_type TEXT NOT NULL DEFAULT 'full',
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  progress JSONB NOT NULL DEFAULT '{}'::jsonb,
  error TEXT,
  available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  leased_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS crawl_jobs_queue_idx ON crawl_jobs(status, available_at);
CREATE TABLE IF NOT EXISTS unauthorized_events (
  bucket_start TIMESTAMPTZ NOT NULL,
  source_hash TEXT NOT NULL,
  domain TEXT NOT NULL,
  request_count INTEGER NOT NULL DEFAULT 0,
  blocked BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY(bucket_start, source_hash, domain)
);
