from .errors import require
from .values import object_body, integer, identifier, canonical, now


class RenderBatches:
    def __init__(self, db, planner, tickets, keys):
        self.db, self.planner, self.tickets, self.keys = db, planner, tickets, keys

    def submit(self, body, key=None):
        object_body(body, ("reel", "revision", "looks", "audio"), ("reel", "looks"))
        looks = body["looks"]
        require(isinstance(looks, list) and 1 <= len(looks) <= 12,
                "batch_size", "A render matrix requires 1..12 looks")
        with self.db.write_unit() as db:
            previous = self.keys.replay(db, "render_matrix", key, body)
            if previous:
                return previous
            snapshots, identities = [], set()
            for item in looks:
                object_body(item, ("id", "version"), ("id",))
                if "version" in item:
                    integer(item["version"], "version", 1)
                snapshot = self.planner.freeze(db, body["reel"], body.get("revision"), item["id"],
                                               item.get("version"), body.get("audio"))
                identity = (snapshot["look"]["id"], snapshot["look"]["version"])
                require(identity not in identities, "duplicate_look", "A look version cannot repeat within a matrix")
                identities.add(identity)
                snapshots.append(snapshot)
            batch_id, created, ids = identifier("matrix"), now(), []
            for snapshot in snapshots:
                ticket_id = identifier("render")
                db.execute("INSERT INTO tickets(id,reel,revision,look_id,look_version,snapshot,fingerprint,state,attempt,created,updated) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (ticket_id, snapshot["reel"], snapshot["revision"], snapshot["look"]["id"],
                            snapshot["look"]["version"], canonical(snapshot), snapshot["fingerprint"], "queued", 1, created, created))
                ids.append(ticket_id)
                self.db.event(db, ticket_id, "matrix_queued", {"matrix": batch_id, "fingerprint": snapshot["fingerprint"]})
            self.db.event(db, batch_id, "matrix_submitted", {"tickets": ids})
            result = {"id": batch_id, "tickets": [self.tickets.get(x, db) for x in ids]}
            return self.keys.record(db, "render_matrix", key, body, result)

    def get(self, batch_id):
        with self.db.read() as db:
            import json
            row = db.execute("SELECT payload FROM events WHERE subject=? AND kind='matrix_submitted' ORDER BY sequence LIMIT 1", (batch_id,)).fetchone()
            require(row is not None, "not_found", "Render matrix does not exist", 404)
            ids = json.loads(row["payload"])["tickets"]
            return self._status(db, batch_id, ids)

    def _status(self, db, batch_id, ids):
        tickets = [self.tickets.get(x, db) for x in ids]
        states = [x["state"] for x in tickets]
        terminal = {"succeeded", "failed", "cancelled"}
        if all(x == "succeeded" for x in states):
            state = "succeeded"
        elif all(x in terminal for x in states):
            state = "finished_with_failures"
        elif any(x in terminal or x == "running" for x in states):
            state = "running"
        else:
            state = "queued"
        return {"id": batch_id, "state": state, "tickets": tickets,
                "progress": sum(x["progress"] for x in tickets)/len(tickets)}


class DubBatches:
    """Atomic enqueue of one render ticket per language/look; executions fail independently."""

    def __init__(self, db, planner, tickets, keys):
        self.db, self.planner, self.tickets, self.keys = db, planner, tickets, keys

    def submit(self, body, key=None):
        object_body(body, ("reel", "revision", "looks", "dubs"), ("reel", "looks", "dubs"))
        looks = body["looks"]
        dubs = body["dubs"]
        require(isinstance(looks, list) and 1 <= len(looks) <= 12, "batch_size",
                "A dub matrix requires 1..12 looks")
        require(isinstance(dubs, list) and 1 <= len(dubs) <= 16, "dub_batch_size",
                "A dub matrix requires 1..16 language selections")
        with self.db.write_unit() as db:
            previous = self.keys.replay(db, "dub_matrix", key, body)
            if previous:
                return previous
            languages, snapshots, identities = set(), [], set()
            for selection in dubs:
                object_body(selection, ("dub_order", "dub_order_version", "language"),
                            ("dub_order", "language"))
                require(selection["language"] not in languages, "duplicate_dub_language",
                        "A language cannot repeat within a dub matrix")
                languages.add(selection["language"])
                audio = {"mode": "dub", "dub_order": selection["dub_order"],
                         "dub_order_version": selection.get("dub_order_version"),
                         "language": selection["language"]}
                for item in looks:
                    object_body(item, ("id", "version"), ("id",))
                    if "version" in item:
                        integer(item["version"], "version", 1)
                    # Every freeze is validated before any ticket is inserted: no partial batch.
                    snapshot = self.planner.freeze(db, body["reel"], body.get("revision"),
                                                   item["id"], item.get("version"), audio)
                    identity = (selection["language"], snapshot["look"]["id"], snapshot["look"]["version"])
                    require(identity not in identities, "duplicate_look",
                            "A look/language combination cannot repeat within a matrix")
                    identities.add(identity)
                    snapshots.append(snapshot)
            batch_id, created, ids = identifier("dubmatrix"), now(), []
            for snapshot in snapshots:
                ticket_id = identifier("render")
                db.execute("INSERT INTO tickets(id,reel,revision,look_id,look_version,snapshot,fingerprint,state,attempt,created,updated) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (ticket_id, snapshot["reel"], snapshot["revision"], snapshot["look"]["id"],
                            snapshot["look"]["version"], canonical(snapshot), snapshot["fingerprint"], "queued", 1, created, created))
                ids.append(ticket_id)
                self.db.event(db, ticket_id, "dub_matrix_queued",
                              {"matrix": batch_id, "language": snapshot["audio"]["language"],
                               "fingerprint": snapshot["fingerprint"]})
            self.db.event(db, batch_id, "dub_matrix_submitted", {"tickets": ids})
            result = {"id": batch_id, "tickets": [self.tickets.get(x, db) for x in ids]}
            return self.keys.record(db, "dub_matrix", key, body, result)

    def get(self, batch_id):
        with self.db.read() as db:
            import json
            row = db.execute("SELECT payload FROM events WHERE subject=? AND kind='dub_matrix_submitted' "
                             "ORDER BY sequence LIMIT 1", (batch_id,)).fetchone()
            require(row is not None, "not_found", "Dub matrix does not exist", 404)
            ids = json.loads(row["payload"])["tickets"]
            tickets = [self.tickets.get(x, db) for x in ids]
            status = RenderBatches(self.db, self.planner, self.tickets, self.keys)._status(db, batch_id, ids)
            status["languages"] = sorted({t["snapshot"]["audio"]["language"] for t in tickets})
            return status
