#!/usr/bin/env python3
"""Copy the models MeetingScribe uses into a GitHub release, and write models.json for the app.

Runs in the Models workflow, where Hugging Face is reachable. Stops if a model's licence isn't
one that allows copying. One model is held at a time, because a runner's disk is small.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import model_store  # noqa: E402

TAG = "models-v1"
PART_BYTES = 1900 * 2**20  # GitHub refuses a release file of 2 GB or more
ALLOWED = {"mit", "apache-2.0", "cc-by-4.0"}
# Conversions whose model card names no licence inherit their source's; the source is checked here.
SOURCE = {"mlx-community/whisper-large-v3-turbo": "openai/whisper-large-v3-turbo"}
MODELS = [
    ("mlx-community/whisper-large-v3-turbo", None),
    ("dropbox-dash/faster-whisper-large-v3-turbo", None),
    ("Wespeaker/wespeaker-voxceleb-resnet34-LM", None),
    ("mlx-community/Qwen2.5-14B-Instruct-4bit", None),
    ("mlx-community/Qwen2.5-7B-Instruct-4bit", None),
    ("bartowski/Qwen2.5-14B-Instruct-GGUF", "Qwen2.5-14B-Instruct-Q4_K_M.gguf"),
    ("bartowski/Qwen2.5-7B-Instruct-GGUF", "Qwen2.5-7B-Instruct-Q4_K_M.gguf"),
]
SKIP = {".gitattributes"}


def gh(*args, check=True):
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True)


def licence_of(api, repo):
    for name in (repo, SOURCE.get(repo)):
        if name:
            card = api.model_info(name).card_data
            found = (card.get("license") if card else None) or ""
            if isinstance(found, str) and found:
                return found.lower()
    return ""


def upload(path, repo_env):
    for attempt in range(3):
        r = gh("release", "upload", TAG, str(path), "--clobber", "--repo", repo_env, check=False)
        if r.returncode == 0:
            return
        print(f"upload retry {attempt + 1}: {r.stderr.strip()[:300]}", flush=True)
    raise SystemExit(f"could not upload {path.name}")


def publish_file(source, relative, have, repo_env, work):
    digest, size = model_store.sha256(source), source.stat().st_size
    count = max(1, (size + PART_BYTES - 1) // PART_BYTES)
    parts = []
    with open(source, "rb") as f:
        for n in range(count):
            name = f"{digest[:32]}.{n:03d}"
            length = min(PART_BYTES, size - n * PART_BYTES)
            parts.append({"name": name, "size": length})
            if have.get(name) == length:  # already there from an earlier run
                continue
            piece = work / name
            f.seek(n * PART_BYTES)
            with open(piece, "wb") as out:
                left = length
                while left:
                    chunk = f.read(min(left, model_store.CHUNK))
                    out.write(chunk)
                    left -= len(chunk)
            upload(piece, repo_env)
            piece.unlink()
            have[name] = length
    return {"path": relative, "size": size, "sha256": digest, "parts": parts}


def main():
    from huggingface_hub import HfApi, snapshot_download

    repo_env = os.environ["GITHUB_REPOSITORY"]
    api = HfApi()
    licences = {repo: licence_of(api, repo) for repo, _ in MODELS}
    print(json.dumps(licences, indent=1), flush=True)
    refused = {r: x for r, x in licences.items() if x not in ALLOWED}
    if refused:
        raise SystemExit(f"licence not cleared for copying: {refused}")
    notes = "Model files for MeetingScribe, copied unchanged so the app can install them where Hugging Face is blocked.\n\n" + "\n".join(
        f"- [{repo}](https://huggingface.co/{repo}), licence: {licences[repo]}" for repo, _ in MODELS)
    if gh("release", "view", TAG, "--repo", repo_env, check=False).returncode:
        gh("release", "create", TAG, "--repo", repo_env, "--prerelease", "--latest=false", "--title", "Model files", "--notes", notes)
    else:
        gh("release", "edit", TAG, "--repo", repo_env, "--prerelease", "--latest=false", "--notes", notes)
    listed = json.loads(gh("release", "view", TAG, "--repo", repo_env, "--json", "assets").stdout)["assets"]
    have = {a["name"]: a["size"] for a in listed}
    manifest = {}
    for repo, filename in MODELS:
        revision = api.model_info(repo).sha
        print("copying", repo, revision, flush=True)
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as work:
            work = Path(work)
            folder = work / "model"
            snapshot_download(repo, revision=revision, local_dir=folder, allow_patterns=[filename] if filename else None)
            files = []
            for path in sorted(p for p in folder.rglob("*") if p.is_file() and ".cache" not in p.relative_to(folder).parts and p.name not in SKIP):
                files.append(publish_file(path, path.relative_to(folder).as_posix(), have, repo_env, work))
            shutil.rmtree(folder)
        manifest[repo] = {"revision": revision, "license": licences[repo], "files": files}
    out = ROOT / "models.json"
    out.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    upload(out, repo_env)
    print("done", sum(f["size"] for m in manifest.values() for f in m["files"]) / 2**30, "GB", flush=True)


if __name__ == "__main__":
    main()
