"""本机的 SQLite（tts.db）：生成过的音频（生成历史）、review（评分、问题标签）、任务。角色、权重、评测、盲听、作品在资料库里
（ttsapi/library）。只有 gateway 进程访问，都在事件循环线程上调用。"""
import json
import sqlite3

from ttsapi import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS audio(
  id TEXT PRIMARY KEY, created REAL, text TEXT, character TEXT, engine TEXT, ref_id TEXT,
  gpt TEXT, sovits TEXT, temperature REAL, top_k INTEGER, top_p REAL, score REAL,
  work_id TEXT, keep INTEGER DEFAULT 0, meta TEXT);
CREATE INDEX IF NOT EXISTS audio_created ON audio(created);
CREATE TABLE IF NOT EXISTS reviews(
  audio_id TEXT PRIMARY KEY, rating INTEGER, verdict TEXT, tags TEXT, note TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, created REAL, data TEXT);
CREATE TABLE IF NOT EXISTS cloud_audio_map(
  job_id TEXT NOT NULL, vm_audio_id TEXT NOT NULL, local_audio_id TEXT NOT NULL,
  PRIMARY KEY(job_id, vm_audio_id));
"""

settings.DATA.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(settings.DB_PATH, isolation_level=None, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript(SCHEMA)


def query(sql: str, *args):
    return conn.execute(sql, args).fetchall()


def one(sql: str, *args):
    return conn.execute(sql, args).fetchone()


def run(sql: str, *args):
    conn.execute(sql, args)


def put_doc(table: str, key: str, created: float, doc: dict):
    run(f"INSERT OR REPLACE INTO {table}(id, created, data) VALUES(?, ?, ?)", key, created,
        json.dumps(doc, ensure_ascii=False))


def get_doc(table: str, key: str):
    row = one(f"SELECT data FROM {table} WHERE id=?", key)
    if row is None:
        raise KeyError(key)
    return json.loads(row["data"])
