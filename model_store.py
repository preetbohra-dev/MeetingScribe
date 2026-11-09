"""Fetch models from this project's own GitHub release, for networks that block Hugging Face.

GitHub limits a release file to about 2 GB, so large models are published in parts. models.json
ships inside the app and pins every file's SHA-256, so what arrives is checked against what this
version of the app expects, wherever it came from. Files land in the Hugging Face cache layout,
which is where the rest of the program already looks when it loads a model offline.
"""

import hashlib
import json
import shutil
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

RELEASE = "https://github.com/preetbohra-dev/MeetingScribe/releases/download/models-v1/"
CHUNK = 1 << 20
ATTEMPTS = 5
SPARE_BYTES = 2 << 30  # room to spare once the models are in
BLOCKED_HELP = "If this is a work computer, ask IT to allow github.com and release-assets.githubusercontent.com."


class StoreError(Exception):
    """Worded for the person."""


def load_manifest(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def wanted_files(manifest, repo, filename):
    return [f for f in manifest.get(repo, {}).get("files", []) if filename in (None, f["path"])]


def plan_size(manifest, plan):
    """Total bytes for the plan, or None if any of it isn't in this project's release."""
    sizes = [sum(f["size"] for f in wanted_files(manifest, repo, name)) for repo, name in plan]
    return sum(sizes) if all(sizes) else None


def check_space(hub, needed):
    hub = Path(hub)
    hub.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(hub).free
    if free < needed + SPARE_BYTES:
        raise StoreError(f"Not enough disk space: the models need {(needed + SPARE_BYTES) / 2**30:.1f} GB free, and there is {free / 2**30:.1f} GB.")


def context(ca_bundle=""):
    """The computer's own trust store, so a certificate IT installed for its proxy is accepted."""
    try:
        import truststore
    except ImportError:
        ctx = ssl.create_default_context()
    else:
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if ca_bundle:
        ctx.load_verify_locations(cafile=str(Path(ca_bundle).expanduser()))
    return ctx


def explain(e):
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 407:
            return f"A proxy on this network asked for a sign-in. {BLOCKED_HELP}"
        if e.code == 403:
            return f"This network refused the download. {BLOCKED_HELP}"
        return f"GitHub answered with error {e.code}."
    reason = getattr(e, "reason", e)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"This network's security certificate isn't trusted by this computer. Ask IT to install it, or set ca_bundle under [jira] in the settings file. {BLOCKED_HELP}"
    return f"Couldn't reach GitHub ({type(reason).__name__}). {BLOCKED_HELP}"


def get_part(opener, url, target, size):
    """One file of the release, continuing a partial one. Network drops are retried."""
    for attempt in range(ATTEMPTS):
        have = target.stat().st_size if target.exists() else 0
        if have == size:
            return
        if have > size:
            target.unlink()
            have = 0
        request = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with opener.open(request, timeout=60) as r:
                mode = "ab" if have and r.status == 206 else "wb"  # a server may ignore the range
                with open(target, mode) as out:
                    shutil.copyfileobj(r, out, CHUNK)
        except urllib.error.HTTPError as e:
            if e.code != 416 and (e.code < 500 or attempt == ATTEMPTS - 1):
                raise StoreError(explain(e)) from e
            if e.code == 416:
                target.unlink(missing_ok=True)
        except OSError as e:
            if attempt == ATTEMPTS - 1:
                raise StoreError(explain(e)) from e
        time.sleep(min(2 ** attempt, 20) if attempt else 0)
    if not target.exists() or target.stat().st_size != size:
        raise StoreError(f"{BLOCKED_HELP} The download kept stopping early.")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def install_file(opener, base, root, snapshot, item):
    relative = Path(item["path"])
    dest = snapshot / relative
    if snapshot.resolve() not in dest.resolve().parents:  # a leading / alone isn't "absolute" on Windows
        raise StoreError("The list of models is damaged. Reinstall MeetingScribe.")
    if dest.is_file() and dest.stat().st_size == item["size"] and sha256(dest) == item["sha256"]:
        return
    parts_dir = root / ".parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    pieces = []
    for part in item["parts"]:
        piece = parts_dir / part["name"]
        get_part(opener, base + part["name"], piece, part["size"])
        pieces.append(piece)
    joined = parts_dir / (item["sha256"] + ".joined")
    with open(joined, "wb") as out:
        for piece in pieces:  # each part goes as it is used, so a model never needs twice its size
            with open(piece, "rb") as f:
                shutil.copyfileobj(f, out, CHUNK)
            piece.unlink()
    if joined.stat().st_size != item["size"] or sha256(joined) != item["sha256"]:
        joined.unlink()
        raise StoreError(f"{relative.name} arrived damaged, so it was thrown away. Try again.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    joined.replace(dest)


def install(manifest, repo, filename, hub, ctx, base=RELEASE):
    """One entry of the plan into the Hugging Face cache. Picks up where a stopped download left off."""
    entry, files = manifest[repo], wanted_files(manifest, repo, filename)
    if not files:
        raise StoreError(f"{repo} isn't in this project's release.")
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    root = Path(hub) / ("models--" + repo.replace("/", "--"))
    snapshot = root / "snapshots" / entry["revision"]
    for item in files:
        install_file(opener, base, root, snapshot, item)
    (root / "refs").mkdir(parents=True, exist_ok=True)
    (root / "refs" / "main").write_text(entry["revision"], encoding="utf-8")
    shutil.rmtree(root / ".parts", ignore_errors=True)
