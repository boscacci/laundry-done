#!/usr/bin/env python
"""Generate immutable voice assets once, recording source and output hashes."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
import boto3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish-bucket")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "services/phone_alerts/messages.json").read_text())
    output = root / "outputs/audio" / manifest["version"]
    output.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is required in the active Conda environment")
    polly = boto3.client("polly", region_name="us-west-2")
    s3 = boto3.client("s3", region_name="us-west-2")
    records = []
    for key, text in manifest["messages"].items():
        identity = hashlib.sha256(
            json.dumps([manifest["voice"], manifest["engine"], text]).encode()
        ).hexdigest()
        target = output / (key + ".wav")
        meta = output / (key + ".json")
        if target.exists():
            if (
                not meta.exists()
                or json.loads(meta.read_text())["source_sha256"] != identity
            ):
                raise SystemExit(
                    "Existing version differs. Increment messages.json version before regenerating."
                )
        else:
            pcm = output / (key + ".pcm")
            response = polly.synthesize_speech(
                Text=text,
                VoiceId=manifest["voice"],
                Engine=manifest["engine"],
                OutputFormat="pcm",
                SampleRate="8000",
            )
            with response["AudioStream"] as audio:
                pcm.write_bytes(audio.read())
            try:
                subprocess.run(
                    [
                        ffmpeg,
                        "-v",
                        "error",
                        "-f",
                        "s16le",
                        "-ar",
                        "8000",
                        "-ac",
                        "1",
                        "-i",
                        str(pcm),
                        "-af",
                        "loudnorm=I=-18:TP=-3:LRA=7",
                        "-ar",
                        "8000",
                        "-ac",
                        "1",
                        "-c:a",
                        "pcm_mulaw",
                        str(target),
                    ],
                    check=True,
                )
            finally:
                pcm.unlink(missing_ok=True)
            # ffprobe gives the duration for mu-law WAV without deprecated audioop.
            duration = float(
                subprocess.check_output(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        str(target),
                    ],
                    text=True,
                )
            )
            if duration > 20:
                target.unlink()
                raise SystemExit("Recording exceeds 20 seconds")
            meta.write_text(
                json.dumps(
                    {
                        "key": key,
                        "source_sha256": identity,
                        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                        "duration_seconds": duration,
                    },
                    indent=2,
                )
                + "\n"
            )
        record = json.loads(meta.read_text())
        records.append(record)
        if hashlib.sha256(target.read_bytes()).hexdigest() != record["sha256"]:
            raise SystemExit("Audio hash mismatch")
        if args.publish_bucket:
            obj = f"laundry/{manifest['version']}/{key}.wav"
            try:
                old = s3.head_object(Bucket=args.publish_bucket, Key=obj)
                if old.get("Metadata", {}).get("sha256") != record["sha256"]:
                    raise SystemExit("Refusing to overwrite immutable audio")
            except s3.exceptions.ClientError as exc:
                if exc.response["Error"]["Code"] not in {
                    "404",
                    "NoSuchKey",
                    "NotFound",
                }:
                    raise
                s3.put_object(
                    Bucket=args.publish_bucket,
                    Key=obj,
                    Body=target.read_bytes(),
                    ContentType="audio/wav",
                    Metadata={"sha256": record["sha256"]},
                )
    (output / "manifest.json").write_text(json.dumps(records, indent=2) + "\n")
    print(
        json.dumps(
            {
                "recordings": len(records),
                "version": manifest["version"],
                "published": bool(args.publish_bucket),
            }
        )
    )


if __name__ == "__main__":
    main()
