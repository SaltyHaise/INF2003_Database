"""Retry queue for MongoDB writes.

Prescribing and reviewing write to PostgreSQL first (the source of truth for status),
then to MongoDB (the audit trail). The two databases can't share a transaction, so if
the MongoDB write fails, it is saved here and retried on the next request.
"""
import json
import threading
from pathlib import Path

from bson import json_util
from flask import current_app
from pymongo.errors import PyMongoError

from .db import mongo

_lock = threading.Lock()


def _path():
    p = Path(current_app.instance_path)
    p.mkdir(parents=True, exist_ok=True)
    return p / "mongo_outbox.jsonl"


def run(collection, op, *args):
    """Run a MongoDB write now; if it fails, queue it. Returns True if it ran now."""
    try:
        getattr(mongo()[collection], op)(*args)
        return True
    except PyMongoError as e:
        with _lock, _path().open("a", encoding="utf-8") as f:
            f.write(json_util.dumps({"collection": collection, "op": op, "args": list(args)}) + "\n")
        current_app.logger.warning("MongoDB write queued for retry: %s", e)
        return False


def flush():
    p = _path()
    if not p.exists() or p.stat().st_size == 0:
        return
    with _lock:
        lines = p.read_text(encoding="utf-8").splitlines()
        left = []
        for line in lines:
            item = json_util.loads(line)
            try:
                getattr(mongo()[item["collection"]], item["op"])(*item["args"])
            except PyMongoError:
                left.append(line)
        p.write_text("".join(l + "\n" for l in left), encoding="utf-8")


def pending_count():
    p = _path()
    return len(p.read_text(encoding="utf-8").splitlines()) if p.exists() else 0
