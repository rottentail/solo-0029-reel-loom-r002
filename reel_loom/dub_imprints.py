import hashlib
import json
import zipfile
from .errors import require, missing
from .values import identifier, now, canonical, digest, object_body


class DubImprints:
    """Handoff package bundling the original plus every selectable language rendition."""

    def __init__(self, db, blobs, tickets, captions):
        self.db, self.blobs, self.tickets, self.captions = db, blobs, tickets, captions

    def create(self, body):
        object_body(body, ("tickets", "cue_tracks"), ("tickets",))
        ticket_ids = body["tickets"]
        require(isinstance(ticket_ids, list) and 1 <= len(ticket_ids) <= 32 and all(isinstance(x, str) for x in ticket_ids),
                "invalid_tickets", "tickets must be an array of 1..32 identifiers")
        require(len(ticket_ids) == len(set(ticket_ids)), "duplicate_ticket", "Tickets cannot repeat")
        track_ids = body.get("cue_tracks", [])
        require(isinstance(track_ids, list) and len(track_ids) <= 64 and all(isinstance(x, str) for x in track_ids),
                "invalid_tracks", "cue_tracks must be an array of identifiers")
        tickets = [self.tickets.get(ticket_id) for ticket_id in ticket_ids]
        for ticket in tickets:
            require(ticket["state"] == "succeeded", "imprint_state",
                    "Only completed renditions can be packaged", 409)
        first = tickets[0]["snapshot"]
        reel_key = (first["reel"], first["revision"], first["reel_hash"])
        selections, languages = [], set()
        for ticket in tickets:
            snap = ticket["snapshot"]
            require((snap["reel"], snap["revision"], snap["reel_hash"]) == reel_key,
                    "dub_package_revision", "Packaged renditions must share one frozen reel revision")
            audio = snap.get("audio", {"mode": "original"})
            if audio["mode"] == "dub":
                label = audio["language"]
                provenance = {"mode": "dub", "language": audio["language"],
                              "dub_order": snap["dub"]["order"],
                              "dub_order_version": snap["dub"]["order_version"],
                              "dub_order_hash": snap["dub"]["order_hash"],
                              "entries": snap["dub"]["document"]["entries"]}
            else:
                label, provenance = "original", {"mode": "original"}
            require(label not in languages, "duplicate_package_language",
                    "Each selectable audio version can appear only once")
            languages.add(label)
            rendition = next((item for item in ticket["artifacts"] if item["kind"] == "rendition"), None)
            require(rendition is not None, "missing_rendition",
                    "A completed render requires a rendition artifact", 409)
            extension = "mp4" if snap["look"]["spec"]["kind"] == "video" else "wav"
            blob = self.blobs.get(rendition["blob"])
            selections.append({"version": label, "ticket": ticket["id"],
                               "look": {"id": snap["look"]["id"], "version": snap["look"]["version"]},
                               "fingerprint": ticket["fingerprint"],
                               "output_duration_tick": snap.get("output_duration_tick"),
                               "provenance": provenance,
                               "file": {"name": f'{label}/{rendition["id"]}.{extension}',
                                        "blob": rendition["blob"], "size": blob["size"],
                                        "kind": rendition["kind"]}})
        caption = self.captions.project(reel_key[0], reel_key[1], track_ids) if track_ids else None
        manifest = {"schema": 1, "reel": reel_key[0], "revision": reel_key[1],
                    "reel_hash": reel_key[2], "selections": selections, "caption": caption}
        manifest["fingerprint"] = digest(manifest)
        with self.db.read() as db:
            row = db.execute("SELECT id FROM dub_imprints WHERE fingerprint=?",
                             (manifest["fingerprint"],)).fetchone()
            if row:
                return self.get(row["id"], db)
        with self.blobs.temporary(".zip") as package:
            with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
                self._write(archive, "manifest.json", canonical(manifest).encode())
                if caption:
                    self._write(archive, "captions.srt", caption["srt"].encode())
                for item in selections:
                    info = zipfile.ZipInfo(item["file"]["name"], date_time=(1980, 1, 1, 0, 0, 0))
                    info.external_attr = 0o644 << 16
                    with archive.open(info, "w") as target, \
                            self.blobs.resolve(item["file"]["blob"], verify=True).open("rb") as source:
                        for chunk in iter(lambda: source.read(1024*1024), b""):
                            target.write(chunk)
            with self.db.write_unit() as db:
                row = db.execute("SELECT id FROM dub_imprints WHERE fingerprint=?",
                                 (manifest["fingerprint"],)).fetchone()
                if row:
                    return self.get(row["id"], db)
                blob = self.blobs.put(package, db)
                imprint_id = identifier("dubimprint")
                db.execute("INSERT INTO dub_imprints VALUES(?,?,?,?,?)",
                           (imprint_id, canonical(manifest), blob["hash"], manifest["fingerprint"], now()))
                for ticket in tickets:
                    self.db.event(db, ticket["id"], "dub_imprinted",
                                  {"imprint": imprint_id, "blob": blob["hash"]})
                return self.get(imprint_id, db)

    def _write(self, archive, name, content):
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.external_attr = 0o644 << 16
        archive.writestr(info, content)

    def get(self, imprint_id, db=None):
        if db is None:
            with self.db.read() as read:
                return self.get(imprint_id, read)
        row = db.execute("SELECT * FROM dub_imprints WHERE id=?", (imprint_id,)).fetchone()
        if row is None:
            raise missing("dub imprint", imprint_id)
        return {**dict(row), "manifest": json.loads(row["manifest"])}

    def list(self):
        with self.db.read() as db:
            rows = db.execute("SELECT id FROM dub_imprints ORDER BY created DESC,id LIMIT 100").fetchall()
            return [self.get(row["id"], db) for row in rows]

    def verify(self, imprint_id):
        imprint = self.get(imprint_id)
        path = self.blobs.resolve(imprint["blob"], verify=True)
        manifest = imprint["manifest"]
        with zipfile.ZipFile(path) as archive:
            require(json.loads(archive.read("manifest.json")) == manifest,
                    "manifest_mismatch", "Packaged manifest differs from frozen record", 409)
            expected_names = {"manifest.json"} | {item["file"]["name"] for item in manifest["selections"]}
            if manifest["caption"]:
                expected_names.add("captions.srt")
                require(archive.read("captions.srt").decode() == manifest["caption"]["srt"],
                        "caption_mismatch", "Packaged captions differ from frozen projection", 409)
            require(set(archive.namelist()) == expected_names, "package_members",
                    "Unexpected package members", 409)
            verified = []
            for item in manifest["selections"]:
                hasher, size = hashlib.sha256(), 0
                with archive.open(item["file"]["name"]) as content:
                    for chunk in iter(lambda: content.read(1024*1024), b""):
                        hasher.update(chunk)
                        size += len(chunk)
                require(hasher.hexdigest() == item["file"]["blob"] and size == item["file"]["size"],
                        "rendition_mismatch", "Packaged media differs from frozen content", 409)
                verified.append(item["version"])
        return {"id": imprint_id, "valid": True, "verified_versions": verified,
                "fingerprint": imprint["fingerprint"], "blob": imprint["blob"]}
