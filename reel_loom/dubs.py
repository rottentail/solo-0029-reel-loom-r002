import json
from .errors import require, missing, revision_conflict
from .timebase import ticks, TICKS_PER_SECOND
from .values import identifier, text, now, canonical, digest, object_body, integer


class Dubs:
    """Immutable, versioned dubbing recordings (tracks) and their placement on a reel (orders)."""

    def __init__(self, db, assets, keys, settings):
        self.db, self.assets, self.keys, self.settings = db, assets, keys, settings

    # -- dub tracks: versioned collections of recorded takes -----------------

    def normalize_track(self, body, db):
        object_body(body, ("language", "takes"), ("language", "takes"))
        language = text(body["language"], "language", 40)
        require(isinstance(body["takes"], list) and 1 <= len(body["takes"]) <= 64,
                "take_count", "A dub track requires 1..64 takes")
        takes, seen = [], set()
        for raw in body["takes"]:
            object_body(raw, ("asset", "label"), ("asset",))
            asset = self.assets.get(raw["asset"], db, ready=True)
            require(asset["metadata"]["has_audio"], "audio_required",
                    "Dub takes must point at media containing an audio stream")
            require(asset["id"] not in seen, "duplicate_take", "A take cannot be listed twice")
            seen.add(asset["id"])
            takes.append({"id": identifier("take"), "asset": asset["id"],
                          "language": language,
                          "label": text(raw.get("label", asset["label"]), "take label"),
                          "duration_tick": asset["metadata"]["duration_tick"],
                          "blob": asset["blob"]})
        return {"language": language, "takes": takes}

    def create_track(self, body, key=None):
        with self.db.write_unit() as db:
            previous = self.keys.replay(db, "dub_tracks", key, body)
            if previous:
                return previous
            document = self.normalize_track(body, db)
            track_id = identifier("dub")
            db.execute("INSERT INTO dub_tracks VALUES(?,?,?)", (track_id, 1, now()))
            self._write_track_version(db, track_id, 1, document)
            result = self.get_track(track_id, db=db)
            return self.keys.record(db, "dub_tracks", key, body, result)

    def replace_track(self, track_id, expected, body):
        integer(expected, "expected_version", 1)
        with self.db.write_unit() as db:
            current = self.get_track(track_id, db=db)
            if current["head"] != expected:
                raise revision_conflict(expected, current["head"])
            document = self.normalize_track(body, db)
            version = expected + 1
            self._write_track_version(db, track_id, version, document)
            db.execute("UPDATE dub_tracks SET head=? WHERE id=?", (version, track_id))
            return self.get_track(track_id, db=db)

    def _write_track_version(self, db, track_id, version, document):
        db.execute("INSERT INTO dub_track_versions VALUES(?,?,?,?,?)",
                   (track_id, version, canonical(document), digest(document), now()))
        self.db.event(db, track_id, "dub_track_written",
                      {"version": version, "language": document["language"],
                       "takes": len(document["takes"]), "hash": digest(document)})

    def get_track(self, track_id, version=None, db=None):
        if db is None:
            with self.db.read() as read:
                return self.get_track(track_id, version, read)
        row = db.execute("SELECT * FROM dub_tracks WHERE id=?", (track_id,)).fetchone()
        if row is None:
            raise missing("dub track", track_id)
        target = row["head"] if version is None else integer(version, "version", 1)
        ver = db.execute("SELECT * FROM dub_track_versions WHERE track=? AND version=?",
                         (track_id, target)).fetchone()
        if ver is None:
            raise missing("dub track version", target)
        return {"id": track_id, "head": row["head"], "revision": target,
                "document": json.loads(ver["document"]), "hash": ver["hash"],
                "created": row["created"]}

    def list_tracks(self):
        with self.db.read() as db:
            rows = db.execute("SELECT id FROM dub_tracks ORDER BY created DESC,id LIMIT 100").fetchall()
            return [self.get_track(row["id"], db=db) for row in rows]

    def track_history(self, track_id):
        with self.db.read() as db:
            self.get_track(track_id, db=db)
            rows = db.execute("SELECT version,hash,created,document FROM dub_track_versions "
                              "WHERE track=? ORDER BY version DESC LIMIT 100", (track_id,))
            result = []
            for row in rows:
                document = json.loads(row["document"])
                result.append({"version": row["version"], "hash": row["hash"], "created": row["created"],
                               "language": document["language"], "takes": len(document["takes"])})
            return result

    # -- dub orders: placement of takes against a reel revision --------------

    def normalize_order(self, body, db):
        object_body(body, ("reel", "revision", "language", "entries"),
                    ("reel", "language", "entries"))
        language = text(body["language"], "language", 40)
        reel = self._reels.get(body["reel"], body.get("revision"), db)
        reel_duration = reel["document"]["duration_tick"]
        require(isinstance(body["entries"], list) and 1 <= len(body["entries"]) <= 64,
                "entry_count", "A dub order requires 1..64 entries")
        entries, cursor = [], 0
        for raw in body["entries"]:
            object_body(raw, ("track", "track_version", "start"),
                        ("track", "start"))
            start = ticks(raw["start"], "start")
            require(start >= 0, "dub_start", "A dub start cannot be negative")
            track = self.get_track(raw["track"], raw.get("track_version"), db)
            take = self._select_take(track, language)
            require(take["language"] == language, "dub_language",
                    "The selected take does not match the order language")
            end = start + take["duration_tick"]
            entries.append({"track": track["id"], "track_version": track["revision"],
                            "take": take["id"], "asset": take["asset"], "blob": take["blob"],
                            "language": language, "start_tick": start, "end_tick": end,
                            "duration_tick": take["duration_tick"]})
            cursor = max(cursor, end)
        require(cursor <= self.settings.max_seconds * TICKS_PER_SECOND,
                "dub_duration", "Dub program exceeds service duration limit")
        return {"reel": reel["id"], "revision": reel["revision"], "reel_hash": reel["hash"],
                "language": language, "reel_duration_tick": reel_duration,
                "program_duration_tick": cursor, "entries": entries}

    def _select_take(self, track, language):
        document = track["document"]
        require(document["language"] == language, "dub_language",
                "The dub track does not carry this language", 422,
                track=track["id"], expected=language, actual=document["language"])
        require(document["takes"], "dub_take_missing",
                "No usable recording exists for this language")
        return document["takes"][-1]

    def create_order(self, body, key=None):
        with self.db.write_unit() as db:
            previous = self.keys.replay(db, "dub_orders", key, body)
            if previous:
                return previous
            document = self.normalize_order(body, db)
            order_id = identifier("duborder")
            db.execute("INSERT INTO dub_orders VALUES(?,?,?,?)",
                       (order_id, document["reel"], 1, now()))
            self._write_order_version(db, order_id, 1, document)
            result = self.get_order(order_id, db=db)
            return self.keys.record(db, "dub_orders", key, body, result)

    def replace_order(self, order_id, expected, body):
        integer(expected, "expected_version", 1)
        with self.db.write_unit() as db:
            current = self.get_order(order_id, db=db)
            if current["head"] != expected:
                raise revision_conflict(expected, current["head"])
            document = self.normalize_order(body, db)
            require(document["reel"] == current["document"]["reel"], "dub_order_reel",
                    "A dub order stays bound to its original reel")
            version = expected + 1
            self._write_order_version(db, order_id, version, document)
            db.execute("UPDATE dub_orders SET head=? WHERE id=?", (version, order_id))
            return self.get_order(order_id, db=db)

    def _write_order_version(self, db, order_id, version, document):
        db.execute("INSERT INTO dub_order_versions VALUES(?,?,?,?,?)",
                   (order_id, version, canonical(document), digest(document), now()))
        self.db.event(db, order_id, "dub_order_written",
                      {"version": version, "language": document["language"],
                       "entries": len(document["entries"]), "hash": digest(document)})

    def get_order(self, order_id, version=None, db=None):
        if db is None:
            with self.db.read() as read:
                return self.get_order(order_id, version, read)
        row = db.execute("SELECT * FROM dub_orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise missing("dub order", order_id)
        target = row["head"] if version is None else integer(version, "version", 1)
        ver = db.execute("SELECT * FROM dub_order_versions WHERE dub_order=? AND version=?",
                         (order_id, target)).fetchone()
        if ver is None:
            raise missing("dub order version", target)
        return {"id": order_id, "reel": row["reel"], "head": row["head"], "revision": target,
                "document": json.loads(ver["document"]), "hash": ver["hash"],
                "created": row["created"]}

    def list_orders(self):
        with self.db.read() as db:
            rows = db.execute("SELECT id FROM dub_orders ORDER BY created DESC,id LIMIT 100").fetchall()
            return [self.get_order(row["id"], db=db) for row in rows]

    def order_history(self, order_id):
        with self.db.read() as db:
            self.get_order(order_id, db=db)
            rows = db.execute("SELECT version,hash,created,document FROM dub_order_versions "
                              "WHERE dub_order=? ORDER BY version DESC LIMIT 100", (order_id,))
            result = []
            for row in rows:
                document = json.loads(row["document"])
                result.append({"version": row["version"], "hash": row["hash"], "created": row["created"],
                               "language": document["language"], "entries": len(document["entries"]),
                               "program_duration_tick": document["program_duration_tick"]})
            return result

    # frozen reads used by the planner; retired assets are reported explicitly
    def frozen_order(self, db, order_id, version, language):
        order = self.get_order(order_id, version, db)
        document = order["document"]
        require(document["language"] == language, "dub_language",
                "The dub order does not cover this language", 422,
                order=order_id, expected=language, actual=document["language"])
        require(document["entries"], "dub_take_missing",
                "No usable recording exists for this language")
        sources = {}
        for entry in document["entries"]:
            asset = self.assets.get(entry["asset"], db, ready=True)
            require(asset["blob"] == entry["blob"], "dub_blob_changed",
                    "Recorded dub content no longer matches the frozen order")
            sources[asset["id"]] = {"id": asset["id"], "blob": asset["blob"],
                                    "metadata": asset["metadata"]}
        return order, sources

    def bind_reels(self, reels):
        self._reels = reels
        return self
