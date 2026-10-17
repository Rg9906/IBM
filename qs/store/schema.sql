CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, scenario TEXT, seed INTEGER, arm TEXT, config_json TEXT);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, payment_id TEXT, event_ts INTEGER NOT NULL,
    received_ts INTEGER, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY, state TEXT NOT NULL, attempts INTEGER, status_checks INTEGER, updated_ts INTEGER);
CREATE TABLE IF NOT EXISTS segment_features (
    tick_ts INTEGER NOT NULL, segment_key TEXT NOT NULL, features_json TEXT NOT NULL,
    anomaly_score REAL, p_stuck REAL, PRIMARY KEY (tick_ts, segment_key));
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY, segment_key TEXT NOT NULL, state TEXT NOT NULL, opened_ts INTEGER NOT NULL,
    closed_ts INTEGER, severity TEXT);
CREATE TABLE IF NOT EXISTS recommendations (
    rec_id TEXT PRIMARY KEY, incident_id TEXT, action TEXT, params_json TEXT, reasons_json TEXT,
    confidence REAL, status TEXT);
CREATE TABLE IF NOT EXISTS decisions (
    rec_id TEXT, decision TEXT, operator TEXT, decided_ts INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS actions_applied (
    rec_id TEXT, applied_ts INTEGER, params_json TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS model_registry (
    model_name TEXT, version TEXT, sha256 TEXT, trained_on_seed_set TEXT, created_ts TEXT,
    PRIMARY KEY (model_name, version));
