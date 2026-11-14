"""A bounded production walkthrough using generated audiovisual media and HTTP."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
from .config import Settings
from .application import Application
from .api import Api
from .http import MediaHttpServer
from .client import Client
from .errors import LoomError, require

WORKFLOWS = ("ingest", "revisions", "looks", "render", "signal", "waveform", "loudness",
             "sheet", "captions", "imprint", "cancel-retry", "recovery", "resources", "matrix",
             "conformance", "lineage", "dubbing")


def expect_error(action, code):
    try:
        action()
    except LoomError as exc:
        require(exc.code == code, "walkthrough_error", "Unexpected workflow rejection", expected=code, actual=exc.code)
        return exc.as_dict()
    raise RuntimeError("The walkthrough expected an operation to be rejected: " + code)


class Walkthrough:
    def __init__(self, settings):
        self.settings = settings
        self.app = Application(settings, start_workers=False)
        self.server = MediaHttpServer(("127.0.0.1", 0), Api(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.client = Client(f"http://127.0.0.1:{self.server.server_port}/loom/v1")

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.close()

    def setup(self):
        with self.app.blobs.temporary(".mp4") as sample:
            self.app.processes.run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3",
                                    "-f", "lavfi", "-i", "sine=frequency=659:sample_rate=48000:duration=3",
                                    "-af", "volume=enable='between(t,1,1.5)':volume=0",
                                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(sample)])
            self.content = sample.read_bytes()
        self.asset = self.client.ingest(self.content, "harbor-tone")
        self.slices = [{"asset": self.asset["id"], "in": .2, "out": 1.2, "label": "开场"},
                       {"asset": self.asset["id"], "in": 1.6, "out": 2.8, "label": "收束"}]
        self.reel = self.client.create_reel("回声样片", self.slices, "demo-reel")
        self.look = self.client.put_look("preview", {"kind": "video", "width": 320, "height": 180})
        self.audio_look = self.client.put_look("listening", {"kind": "audio", "sample_rate": 48000})
        self.ticket = self.client.render(self.reel["id"], "preview", key="demo-render")
        return self

    def finish(self):
        ticket = self.client.request("GET", "/tickets/"+self.ticket["id"])
        if ticket["state"] == "queued":
            self.app.renderer.run_once()
        ticket = self.client.wait(self.ticket["id"], timeout=5)
        require(ticket["state"] == "succeeded", "walkthrough_render", "Walkthrough render did not succeed", ticket=ticket)
        return ticket

    def make_audio(self, frequency, duration):
        with self.app.blobs.temporary(".wav") as sample:
            self.app.processes.run(["-f", "lavfi", "-i",
                                    f"sine=frequency={frequency}:sample_rate=48000:duration={duration}",
                                    "-c:a", "pcm_s16le", str(sample)])
            return sample.read_bytes()

    def run(self, name):
        c, a = self.client, self.asset["id"]
        if name == "ingest":
            duplicate = c.ingest(self.content, "duplicate-name")
            require(duplicate["id"] == a, "walkthrough_dedup", "Identical ready media should be reused")
            failure = expect_error(lambda: c.ingest(b"not-media", "bad"), "unreadable_media")
            return {"asset": self.asset, "deduplicated": True, "failure": failure}
        if name == "revisions":
            updated = c.replace_reel(self.reel["id"], 1, "回声样片·修订", list(reversed(self.slices)))
            old = c.request("GET", "/reels/"+self.reel["id"]+"?revision=1")
            require(old["hash"] == self.reel["hash"] and updated["revision"] == 2,
                    "walkthrough_revision", "Historical reel changed")
            failure = expect_error(lambda: c.replace_reel(self.reel["id"], 1, "旧稿", self.slices), "revision_conflict")
            return {"head": updated["head"], "historical_hash": old["hash"], "failure": failure}
        if name == "looks":
            new = c.put_look("preview", {"kind": "video", "width": 640, "height": 360}, 1)
            ticket = c.request("GET", "/tickets/"+self.ticket["id"])
            require(ticket["look_version"] == 1 and new["version"] == 2, "walkthrough_look", "Queued look drifted")
            failure = expect_error(lambda: c.put_look("preview", {"kind":"video"}, 1), "revision_conflict")
            return {"new_version": 2, "frozen_version": ticket["look_version"], "failure": failure}
        if name == "render":
            ticket = self.finish()
            body = {"reel": self.reel["id"], "look": "preview"}
            replay = c.request("POST", "/tickets", body, {"Idempotency-Key":"demo-render"})
            require(replay["id"] == ticket["id"], "walkthrough_idempotency", "Render replay changed identity")
            failure = expect_error(lambda: c.request("POST", "/tickets", {**body,"look":"listening"},
                                                   {"Idempotency-Key":"demo-render"}), "idempotency_conflict")
            audio = c.render(self.reel["id"], "listening")
            self.app.renderer.run_once()
            audio = c.wait(audio["id"], 5)
            require(audio["state"] == "succeeded", "walkthrough_audio", "Audio rendition did not succeed")
            return {"video": ticket, "audio": audio, "failure": failure}
        if name in ("signal", "waveform", "loudness"):
            result = c.request("POST", f"/assets/{a}/{name}", {})
            replay = c.request("POST", f"/assets/{a}/{name}", {})
            require(result["id"] == replay["id"], "walkthrough_analysis", "Analysis cache should be content/settings stable")
            failure = expect_error(lambda: c.request("POST", f"/assets/{a}/{name}", {"unsupported": 1}), "unknown_fields")
            return {"analysis": result, "cached": True, "failure": failure}
        if name == "sheet":
            result = c.request("POST", f"/assets/{a}/contact-sheet", {"count": 6, "columns": 3})
            media = c.request("GET", "/artifacts/"+result["id"]+"/content", raw=True)
            require(media.startswith(b"\xff\xd8"), "walkthrough_sheet", "Contact sheet is not JPEG")
            failure = expect_error(lambda: c.request("POST", f"/assets/{a}/contact-sheet", {"count":1,"columns":3}), "sheet_layout")
            return {"artifact": result, "bytes": len(media), "failure": failure}
        if name == "captions":
            track = c.request("POST", f"/assets/{a}/cues", {"language":"zh-CN",
                              "srt":"1\n00:00:00,000 --> 00:00:02,900\n潮声\n"})
            projected = c.request("POST", f'/reels/{self.reel["id"]}/captions', {"revision":1,"tracks":[track["id"]]})
            require(len(projected["cues"]) == 2, "walkthrough_captions", "A cue spanning cuts must project to both slices")
            failure = expect_error(lambda: c.request("POST", f"/assets/{a}/cues", {"language":"zh","srt":"broken"}), "cue_format")
            return {"projection": projected, "track": track, "failure": failure}
        if name == "imprint":
            failure = expect_error(lambda: c.imprint(self.ticket["id"]), "imprint_state")
            ticket = self.finish()
            imprint = c.imprint(ticket["id"])
            verified = c.request("GET", f'/imprints/{imprint["id"]}/verify')
            self.client.replace_reel(self.reel["id"], 1, "新的草剪", list(reversed(self.slices)))
            unchanged = c.imprint(ticket["id"])
            require(imprint["id"] == unchanged["id"], "walkthrough_imprint", "Upstream editing changed frozen imprint")
            return {"imprint": imprint, "verified": verified, "failure": failure}
        if name == "cancel-retry":
            cancelled = c.request("POST", f'/tickets/{self.ticket["id"]}/cancel')
            require(cancelled["state"] == "cancelled", "walkthrough_cancel", "Queued render did not cancel")
            retried = c.request("POST", f'/tickets/{self.ticket["id"]}/retry')
            ticket = self.finish()
            failure = expect_error(lambda: c.request("POST", f'/tickets/{self.ticket["id"]}/retry'), "retry_state")
            require(retried["attempt"] == 2, "walkthrough_attempt", "Retry attempt was not incremented")
            return {"ticket": ticket, "failure": failure}
        if name == "recovery":
            claimed = self.app.tickets.claim()
            self.close()
            with Application(self.settings, start_workers=False) as restarted:
                recovered = restarted.tickets.get(claimed["id"])
                require(recovered["state"] == "failed" and recovered["error"]["error"] == "worker_interrupted",
                        "walkthrough_recovery", "Interrupted render was not recovered")
                restarted.tickets.retry(claimed["id"])
                finished = restarted.renderer.run_once()
                require(finished["state"] == "succeeded", "walkthrough_recovery", "Recovered render could not retry")
                return {"interrupted": recovered, "retried_state": finished["state"]}
        if name == "resources":
            c.request("POST", f"/assets/{a}/retire")
            failure = expect_error(lambda: c.create_reel("退役来源", self.slices), "asset_retired")
            ticket = self.finish()
            sweep = c.request("POST", "/resources/sweep", {"dry_run":False})
            status = c.request("GET", "/resources")
            require(status["consistent"] and not sweep["removed"], "walkthrough_resources", "Referenced source was collected")
            return {"reconcile":status,"sweep":sweep,"render_state":ticket["state"],"failure":failure}
        if name == "matrix":
            body = {"reel":self.reel["id"],"looks":[{"id":"preview"},{"id":"listening"}]}
            matrix = c.request("POST", "/matrices", body, {"Idempotency-Key":"demo-matrix"})
            before = len(c.request("GET", "/tickets")["items"])
            failure = expect_error(lambda: c.request("POST", "/matrices", {**body,"looks":[{"id":"preview"},{"id":"missing-look"}]}), "not_found")
            require(len(c.request("GET", "/tickets")["items"]) == before, "walkthrough_matrix", "Invalid matrix committed partial work")
            while self.app.renderer.run_once() is not None:
                pass
            finished = c.request("GET", "/matrices/"+matrix["id"])
            require(finished["state"] == "succeeded", "walkthrough_matrix", "Matrix did not complete")
            return {"matrix": finished,"failure":failure}
        if name == "conformance":
            ticket = self.finish()
            artifact = ticket["artifacts"][0]
            result = c.request("GET", f'/artifacts/{artifact["id"]}/conformance')
            require(result["valid"], "walkthrough_conformance", "Rendered media violates frozen look", report=result)
            failure = expect_error(lambda: c.request("GET", "/artifacts/missing/conformance"), "not_found")
            return {"conformance":result,"failure":failure}
        if name == "lineage":
            self.finish()
            imprint = c.imprint(self.ticket["id"])
            result = c.request("GET", f'/tickets/{self.ticket["id"]}/lineage')
            require(any(n["id"] == imprint["id"] for n in result["nodes"]), "walkthrough_lineage", "Imprint missing from lineage")
            failure = expect_error(lambda: c.request("GET", "/tickets/missing/lineage"), "not_found")
            return {"lineage":result,"failure":failure}
        if name == "dubbing":
            en_audio = c.ingest(self.make_audio(440, 1.4), "en-take")
            jp_audio = c.ingest(self.make_audio(880, 4.0), "jp-take")
            en_track = c.create_dub_track("en", [{"asset": en_audio["id"], "label": "英语整句"}], "demo-dub-en")
            jp_track = c.create_dub_track("ja", [{"asset": jp_audio["id"], "label": "日语长句"}], "demo-dub-ja")
            order = c.create_dub_order({"reel": self.reel["id"], "revision": 1, "language": "en",
                                        "entries": [{"track": en_track["id"], "start": 0.1}]},
                                       "demo-dub-order")
            # A different language cannot be attached to an order that does not cover it.
            failure = expect_error(lambda: c.create_dub_order(
                {"reel": self.reel["id"], "revision": 1, "language": "ja",
                 "entries": [{"track": en_track["id"], "start": 0.1}]}), "dub_language")
            # Track and order are versioned; stale If-Match is rejected.
            conflict = expect_error(lambda: c.replace_dub_track(
                en_track["id"], 9, "en", [{"asset": en_audio["id"]}]), "revision_conflict")
            c.replace_dub_track(en_track["id"], 1, "en", [{"asset": en_audio["id"]}])
            en2 = c.request("GET", f'/dub-tracks/{en_track["id"]}?version=2')
            require(en2["head"] == 2, "walkthrough_dub_version", "Dub track did not advance to version 2")
            # The long Japanese take starts at 0.05s and runs past the 2.2s reel: it must not be cut.
            jp_order = c.create_dub_order({"reel": self.reel["id"], "revision": 1, "language": "ja",
                                           "entries": [{"track": jp_track["id"], "start": 0.05}]})
            reel_duration = self.reel["document"]["duration_tick"]
            require(jp_order["document"]["program_duration_tick"] > reel_duration,
                    "walkthrough_dub_length", "A longer take must extend the program rather than truncate")
            original = c.render(self.reel["id"], "listening", key="dub-original")
            en_ticket = c.render(self.reel["id"], "listening",
                                 audio={"mode": "dub", "dub_order": order["id"], "language": "en"})
            jp_ticket = c.render(self.reel["id"], "listening",
                                 audio={"mode": "dub", "dub_order": jp_order["id"], "language": "ja"})
            # Atomic multi-language matrix: one missing recording rejects the whole submission.
            bad_matrix = expect_error(lambda: c.dub_matrix(
                {"reel": self.reel["id"], "looks": [{"id": "listening"}],
                 "dubs": [{"dub_order": order["id"], "language": "en"},
                          {"dub_order": order["id"], "language": "ja"}]}), "dub_language")
            matrix = c.dub_matrix({"reel": self.reel["id"], "looks": [{"id": "listening"}],
                                   "dubs": [{"dub_order": order["id"], "language": "en"},
                                            {"dub_order": jp_order["id"], "language": "ja"}]},
                                  "demo-dub-matrix")
            before = len(c.request("GET", "/tickets")["items"])
            expect_error(lambda: c.dub_matrix(
                {"reel": self.reel["id"], "looks": [{"id": "missing"}],
                 "dubs": [{"dub_order": order["id"], "language": "en"}]}), "not_found")
            require(len(c.request("GET", "/tickets")["items"]) == before,
                    "walkthrough_dub_matrix", "Invalid dub matrix committed partial tickets")
            while self.app.renderer.run_once() is not None:
                pass
            rendered = {t["id"]: c.wait(t["id"], 10) for t in matrix["tickets"]}
            require(all(t["state"] == "succeeded" for t in rendered.values()),
                    "walkthrough_dub_render", "A language rendition failed in the matrix")
            original = c.wait(original["id"], 10)
            en_ticket = c.wait(en_ticket["id"], 10)
            jp_ticket = c.wait(jp_ticket["id"], 10)
            # The legacy original request keeps the reel duration; dubbed output keeps the full sentence.
            from .timebase import seconds as to_seconds
            orig_meta = original["artifacts"][0]["metadata"]
            jp_meta = jp_ticket["artifacts"][0]["metadata"]
            require(abs(to_seconds(orig_meta["duration_tick"]) - to_seconds(reel_duration)) <= 0.15,
                    "walkthrough_original_duration", "Original audio rendition changed duration")
            require(to_seconds(jp_meta["duration_tick"]) > to_seconds(reel_duration) + 1.0,
                    "walkthrough_dub_truncation", "The long Japanese sentence was truncated")
            require(en_ticket["snapshot"]["dub"]["order_hash"] == order["hash"],
                    "walkthrough_dub_freeze", "Render snapshot lost the frozen dub order")
            # Conformance validates the dubbed full-duration output against its frozen plan.
            conform = c.request("GET", f'/artifacts/{jp_ticket["artifacts"][0]["id"]}/conformance')
            require(conform["valid"], "walkthrough_dub_conformance", "Dub rendition failed conformance", report=conform)
            # A ticket frozen before retirement stays reproducible even once its recording is retired.
            frozen = c.render(self.reel["id"], "listening",
                              audio={"mode": "dub", "dub_order": order["id"], "language": "en"},
                              key="demo-dub-frozen")
            require(frozen["state"] == "queued", "walkthrough_dub_queued", "Frozen ticket should wait in the queue")
            c.request("POST", f"/assets/{en_audio['id']}/retire")
            retired_failure = expect_error(lambda: c.render(
                self.reel["id"], "listening",
                audio={"mode": "dub", "dub_order": order["id"], "language": "en"}), "asset_retired")
            self.app.renderer.run_once()
            recovered = c.wait(frozen["id"], 10)
            require(recovered["state"] == "succeeded", "walkthrough_dub_repro",
                    "A queued/frozen dub could not be reproduced after retirement")
            # A failed/cancelled frozen ticket can also be retried against its original snapshot.
            victim = c.render(self.reel["id"], "listening",
                              audio={"mode": "dub", "dub_order": jp_order["id"], "language": "ja"})
            c.request("POST", f"/tickets/{victim['id']}/cancel")
            retried = c.request("POST", f"/tickets/{victim['id']}/retry")
            self.app.renderer.run_once()
            retried_done = c.wait(victim["id"], 10)
            require(retried_done["state"] == "succeeded" and retried["attempt"] == 2,
                    "walkthrough_dub_retry", "Cancelled dub ticket did not retry on its frozen snapshot")
            c.request("POST", f"/assets/{en_audio['id']}/restore")
            # Handoff package lets the receiver pick original, en or ja, with provenance per version.
            imprint = c.dub_imprint([original["id"], en_ticket["id"], jp_ticket["id"]])
            require({s["version"] for s in imprint["manifest"]["selections"]} == {"original", "en", "ja"},
                    "walkthrough_dub_handoff", "Handoff package is missing a selectable version")
            verified = c.request("GET", f'/dub-imprints/{imprint["id"]}/verify')
            require(set(verified["verified_versions"]) == {"original", "en", "ja"},
                    "walkthrough_dub_verify", "Dub imprint verification incomplete")
            lineage = c.request("GET", f'/tickets/{jp_ticket["id"]}/lineage')
            require(any(n["kind"] == "dub_order" for n in lineage["nodes"]),
                    "walkthrough_dub_lineage", "Dub order missing from lineage")
            return {"matrix": matrix["id"], "original_seconds": to_seconds(orig_meta["duration_tick"]),
                    "japanese_seconds": to_seconds(jp_meta["duration_tick"]),
                    "imprint": imprint["id"], "failures": [failure, conflict, bad_matrix, retired_failure]}
        raise ValueError("Unknown walkthrough: " + name)


def main():
    parser = argparse.ArgumentParser(description="Run a bounded audiovisual production walkthrough")
    parser.add_argument("--workflow", choices=WORKFLOWS, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="reel-loom-demo-") as folder:
        settings = replace(Settings.from_env(), root=Path(folder), render_timeout=30)
        demo = Walkthrough(settings)
        try:
            demo.setup()
            result = demo.run(args.workflow)
            print(json.dumps({"workflow":args.workflow,"ok":True,"result":result}, ensure_ascii=False))
        finally:
            if args.workflow != "recovery":
                demo.close()


if __name__ == "__main__":
    main()
