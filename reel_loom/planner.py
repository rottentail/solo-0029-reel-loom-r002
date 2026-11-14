from .errors import require
from .values import digest
from .timebase import decimal, seconds
from .probe import select_stream


class Planner:
    def __init__(self, assets, reels, looks, dubs=None):
        self.assets, self.reels, self.looks, self.dubs = assets, reels, looks, dubs

    def freeze(self, db, reel_id, revision, look_id, version, audio=None):
        audio = self._normalize_audio(audio)
        reel = self.reels.get(reel_id, revision, db)
        look = self.looks.get(look_id, version, db)
        closure = {}
        for item in reel["document"]["slices"]:
            if item["asset"] not in closure:
                asset = self.assets.get(item["asset"], db, ready=True)
                closure[asset["id"]] = {"id": asset["id"], "blob": asset["blob"], "metadata": asset["metadata"]}
        if look["spec"]["kind"] == "video":
            require(all(x["metadata"]["has_video"] for x in closure.values()),
                    "video_required", "Video looks require video in every slice")
        elif audio["mode"] == "original":
            require(all(x["metadata"]["has_audio"] for x in closure.values()),
                    "audio_required", "Audio looks require audio in every slice")
        dub_block, output_duration = None, reel["document"]["duration_tick"]
        if audio["mode"] == "dub":
            require(self.dubs is not None, "dub_unsupported", "Dubbing workflow is unavailable")
            order, dub_sources = self.dubs.frozen_order(
                db, audio["dub_order"], audio.get("dub_order_version"), audio["language"])
            require(order["document"]["reel"] == reel_id and order["document"]["revision"] == reel["revision"],
                    "dub_order_revision", "The dub order was produced for another reel revision")
            closure.update(dub_sources)
            dub_block = {"mode": "dub", "language": audio["language"],
                         "order": order["id"], "order_version": order["revision"],
                         "order_hash": order["hash"], "document": order["document"]}
            output_duration = max(output_duration, order["document"]["program_duration_tick"])
        snapshot = {"schema": 2, "reel": reel_id, "revision": reel["revision"],
                    "reel_hash": reel["hash"], "document": reel["document"],
                    "look": {"id": look["id"], "version": look["version"], "spec": look["spec"], "hash": look["hash"]},
                    "audio": audio, "dub": dub_block,
                    "output_duration_tick": output_duration,
                    "sources": closure}
        return {**snapshot, "fingerprint": digest(snapshot)}

    def _normalize_audio(self, audio):
        if audio is None:
            return {"mode": "original"}
        require(isinstance(audio, dict), "invalid_audio", "audio must be an object")
        mode = audio.get("mode", "original")
        require(mode in ("original", "dub"), "audio_mode", "audio mode must be original or dub")
        if mode == "original":
            require(set(audio) <= {"mode"}, "unknown_fields", "Original audio takes no extra options")
            return {"mode": "original"}
        require({"dub_order", "language"} <= set(audio), "missing_field",
                "Dub audio requires dub_order and language")
        require(set(audio) <= {"mode", "dub_order", "dub_order_version", "language"},
                "unknown_fields", "Unknown dub audio options")
        return {"mode": "dub", "dub_order": audio["dub_order"],
                "dub_order_version": audio.get("dub_order_version"),
                "language": audio["language"]}

    def compile(self, snapshot, resolve, output):
        if snapshot.get("audio", {}).get("mode") == "dub":
            return self._compile_dub(snapshot, resolve, output)
        return self._compile_original(snapshot, resolve, output)

    def _compile_original(self, snapshot, resolve, output):
        spec = snapshot["look"]["spec"]
        args, filters, segments = [], [], []
        for i, item in enumerate(snapshot["document"]["slices"]):
            asset = snapshot["sources"][item["asset"]]
            args += ["-protocol_whitelist", "file,pipe", "-i", str(resolve(asset["blob"]))]
            a, b = decimal(item["in_tick"]), decimal(item["out_tick"])
            duration = seconds(item["out_tick"]-item["in_tick"])
            audio = select_stream(asset["metadata"], "audio")
            if spec["kind"] == "video":
                video = select_stream(asset["metadata"], "video")
                if spec["fit"] == "contain":
                    sizing = (f'scale={spec["width"]}:{spec["height"]}:force_original_aspect_ratio=decrease,'
                              f'pad={spec["width"]}:{spec["height"]}:(ow-iw)/2:(oh-ih)/2')
                else:
                    sizing = (f'scale={spec["width"]}:{spec["height"]}:force_original_aspect_ratio=increase,'
                              f'crop={spec["width"]}:{spec["height"]}')
                filters.append(f'[{i}:{video["index"]}]trim=start={a}:end={b},setpts=PTS-STARTPTS,'
                               f'{sizing},setsar=1,fps={spec["frame_rate"]},format=yuv420p[v{i}]')
            layout = "mono" if spec["channels"] == 1 else "stereo"
            if audio:
                filters.append(f'[{i}:{audio["index"]}]atrim=start={a}:end={b},asetpts=PTS-STARTPTS,'
                               f'aresample={spec["sample_rate"]},aformat=sample_fmts=fltp:channel_layouts={layout},'
                               f'apad,atrim=duration={duration:.6f},volume={spec["gain_db"]}dB[a{i}]')
            else:
                filters.append(f'anullsrc=r={spec["sample_rate"]}:cl={layout},atrim=duration={duration:.6f}[a{i}]')
            segments.append((f"[v{i}]" if spec["kind"] == "video" else "") + f"[a{i}]")
        count = len(segments)
        if spec["kind"] == "video":
            filters.append("".join(segments) + f"concat=n={count}:v=1:a=1[vjoined][aout]")
            filters.append(f'[vjoined]fps={spec["frame_rate"]},tpad=stop_mode=clone:stop_duration=0.1,'
                           f'trim=duration={seconds(snapshot["document"]["duration_tick"]):.6f}[vout]')
            maps = ["-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", str(spec["crf"]), "-c:a", "aac", "-movflags", "+faststart"]
        else:
            filters.append("".join(segments) + f"concat=n={count}:v=0:a=1[aout]")
            maps = ["-map", "[aout]", "-c:a", "pcm_s16le"]
        return args + ["-filter_complex", ";".join(filters)] + maps + [str(output)]

    def _compile_dub(self, snapshot, resolve, output):
        # Cut points stay exactly where the revision places them; a longer take only extends the
        # tail (cloned last frame / silence). No take is truncated and no shot is moved.
        spec = snapshot["look"]["spec"]
        document = snapshot["document"]
        reel_duration = seconds(document["duration_tick"])
        output_duration = seconds(snapshot["output_duration_tick"])
        layout = "mono" if spec["channels"] == 1 else "stereo"
        args, filters, segments = [], [], []
        slice_count = len(document["slices"])
        for i, item in enumerate(document["slices"]):
            asset = snapshot["sources"][item["asset"]]
            args += ["-protocol_whitelist", "file,pipe", "-i", str(resolve(asset["blob"]))]
            a, b = decimal(item["in_tick"]), decimal(item["out_tick"])
            duration = seconds(item["out_tick"]-item["in_tick"])
            if spec["kind"] == "video":
                video = select_stream(asset["metadata"], "video")
                if spec["fit"] == "contain":
                    sizing = (f'scale={spec["width"]}:{spec["height"]}:force_original_aspect_ratio=decrease,'
                              f'pad={spec["width"]}:{spec["height"]}:(ow-iw)/2:(oh-ih)/2')
                else:
                    sizing = (f'scale={spec["width"]}:{spec["height"]}:force_original_aspect_ratio=increase,'
                              f'crop={spec["width"]}:{spec["height"]}')
                filters.append(f'[{i}:{video["index"]}]trim=start={a}:end={b},setpts=PTS-STARTPTS,'
                               f'{sizing},setsar=1,fps={spec["frame_rate"]},format=yuv420p[v{i}]')
            # Original dialogue is replaced by a timed silence bed, keeping slice timing identical.
            filters.append(f'anullsrc=r={spec["sample_rate"]}:cl={layout},'
                           f'atrim=duration={duration:.6f}[a{i}]')
            segments.append((f"[v{i}]" if spec["kind"] == "video" else "") + f"[a{i}]")
        dub_labels = []
        for offset, entry in enumerate(snapshot["dub"]["document"]["entries"]):
            index = slice_count + offset
            asset = snapshot["sources"][entry["asset"]]
            args += ["-protocol_whitelist", "file,pipe", "-i", str(resolve(asset["blob"]))]
            audio = select_stream(asset["metadata"], "audio")
            delay_ms = max(0, round(entry["start_tick"] / 1000))
            label = f"[d{offset}]"
            filters.append(f'[{index}:{audio["index"]}]aresample={spec["sample_rate"]},'
                           f'aformat=sample_fmts=fltp:channel_layouts={layout},'
                           f'volume={spec["gain_db"]}dB,adelay={delay_ms}:all=1,'
                           f'apad,atrim=duration={output_duration:.6f}{label}')
            dub_labels.append(label)
        if spec["kind"] == "video":
            filters.append("".join(segments) + f"concat=n={slice_count}:v=1:a=1[vjoined][bed0]")
            extend = max(0.0, output_duration-reel_duration) + 0.1
            filters.append(f'[vjoined]fps={spec["frame_rate"]},'
                           f'tpad=stop_mode=clone:stop_duration={extend:.6f},'
                           f'trim=duration={output_duration:.6f}[vout]')
        else:
            filters.append("".join(segments) + f"concat=n={slice_count}:v=0:a=1[bed0]")
        chain = "[bed0]" + "".join(dub_labels)
        filters.append(f'{chain}amix=inputs={1+len(dub_labels)}:normalize=0:dropout_transition=0,'
                       f'apad,atrim=duration={output_duration:.6f}[aout]')
        if spec["kind"] == "video":
            maps = ["-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", str(spec["crf"]), "-c:a", "aac", "-movflags", "+faststart"]
        else:
            maps = ["-map", "[aout]", "-c:a", "pcm_s16le"]
        return args + ["-filter_complex", ";".join(filters)] + maps + [str(output)]
