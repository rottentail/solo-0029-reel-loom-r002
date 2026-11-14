import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from .errors import LoomError


class Client:
    def __init__(self, base_url="http://127.0.0.1:8729/loom/v1", timeout=190):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method, path, body=None, headers=None, raw=False):
        headers = dict(headers or {})
        if isinstance(body, bytes):
            content = body
            headers.setdefault("Content-Type", "application/octet-stream")
        elif body is not None:
            content = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            headers.setdefault("Content-Type", "application/json")
        else:
            content = b"" if method in ("POST", "PUT") else None
        request = Request(self.base_url+path, data=content, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = response.read()
                return data if raw else json.loads(data)
        except HTTPError as exc:
            try:
                value = json.loads(exc.read())
            except (ValueError, UnicodeError):
                value = {"error": "http_failure", "detail": str(exc)}
            raise LoomError(value.get("error", "http_failure"), value.get("detail", str(exc)),
                            exc.code, value.get("context")) from exc

    def ingest(self, content, label="media"):
        # HTTP header labels are ASCII; descriptive Unicode labels belong in reel slices.
        return self.request("POST", "/assets", content, {"X-Media-Label": label})

    def create_reel(self, title, slices, key=None):
        return self.request("POST", "/reels", {"title": title, "slices": slices},
                            {"Idempotency-Key": key} if key else {})

    def replace_reel(self, reel_id, revision, title, slices):
        return self.request("PUT", "/reels/"+reel_id, {"title": title, "slices": slices}, {"If-Match": str(revision)})

    def put_look(self, look_id, spec, expected=0):
        return self.request("PUT", "/looks/"+look_id, spec, {"If-Match": str(expected)})

    def render(self, reel_id, look_id, revision=None, look_version=None, key=None, audio=None):
        body = {"reel": reel_id, "look": look_id}
        if revision is not None:
            body["revision"] = revision
        if look_version is not None:
            body["look_version"] = look_version
        if audio is not None:
            body["audio"] = audio
        return self.request("POST", "/tickets", body, {"Idempotency-Key": key} if key else {})

    def create_dub_track(self, language, takes, key=None):
        return self.request("POST", "/dub-tracks", {"language": language, "takes": takes},
                            {"Idempotency-Key": key} if key else {})

    def replace_dub_track(self, track_id, expected, language, takes):
        return self.request("PUT", "/dub-tracks/"+track_id,
                            {"language": language, "takes": takes}, {"If-Match": str(expected)})

    def create_dub_order(self, body, key=None):
        return self.request("POST", "/dub-orders", body,
                            {"Idempotency-Key": key} if key else {})

    def replace_dub_order(self, order_id, expected, body):
        return self.request("PUT", "/dub-orders/"+order_id, body, {"If-Match": str(expected)})

    def dub_matrix(self, body, key=None):
        return self.request("POST", "/dub-matrices", body,
                            {"Idempotency-Key": key} if key else {})

    def dub_imprint(self, ticket_ids, cue_tracks=None):
        return self.request("POST", "/dub-imprints",
                            {"tickets": ticket_ids, "cue_tracks": cue_tracks or []})

    def wait(self, ticket_id, timeout=185):
        deadline = time.monotonic()+timeout
        while True:
            ticket = self.request("GET", "/tickets/"+ticket_id)
            if ticket["state"] in ("succeeded", "failed", "cancelled"):
                return ticket
            if time.monotonic() >= deadline:
                raise TimeoutError("Render did not reach a terminal state before the client deadline")
            time.sleep(.1)

    def imprint(self, ticket_id, cue_tracks=None):
        return self.request("POST", "/tickets/"+ticket_id+"/imprints", {"cue_tracks": cue_tracks or []})

    def download(self, path, destination):
        request = Request(self.base_url+path)
        with urlopen(request, timeout=self.timeout) as response, destination.open("wb") as output:
            while True:
                chunk = response.read(1024*1024)
                if not chunk:
                    break
                output.write(chunk)
        return destination
