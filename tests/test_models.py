"""Which models a computer gets, and fetching them from this project's release.

    python -m pytest tests
"""

import hashlib
import http.client
import http.server
import importlib.util
import json
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import meeting_scribe as ms  # noqa: E402
import meetingscribe_app as app  # noqa: E402
import model_store as store  # noqa: E402

MLX_14, GGUF_14 = ms.LLM_TIERS[0][1:]
MLX_7, GGUF_7 = ms.LLM_TIERS[1][1:]
REV = "a" * 40


@pytest.fixture
def config(tmp_path, monkeypatch):
    def make(text="", ram=32.0):
        p = tmp_path / "config.toml"
        p.write_text(f'notify = false\nwork_dir = {json.dumps(str(tmp_path / "w"))}\noutput_dir = {json.dumps(str(tmp_path / "n"))}\nlog_dir = {json.dumps(str(tmp_path / "l"))}\n{text}\n', encoding="utf-8")
        monkeypatch.setenv("MEETINGSCRIBE_CONFIG", str(p))
        monkeypatch.setattr(ms.plat, "ram_gb", lambda: ram)
        (tmp_path / "w").mkdir(exist_ok=True)
        return ms.load_config()
    return make


MLX = 'llm_backend = "mlx"\n'
GGUF = 'llm_backend = "llamacpp"\n'


# -- only what the computer can run


@pytest.mark.parametrize("ram,model", [(64, MLX_14), (24, MLX_14), (23.5, MLX_14), (18, MLX_7), (16, MLX_7), (15.7, MLX_7)])
def test_a_mac_gets_the_model_its_memory_allows(config, ram, model):
    cfg = config(MLX + f'mlx_llm_model = "{MLX_14}"', ram)
    assert cfg["mlx_llm_model"] == model and cfg["llm_fits"] is True
    assert [model, None] in [list(x) for x in ms.model_plan(cfg)]


@pytest.mark.parametrize("ram,model", [(32, GGUF_14), (16, GGUF_7)])
def test_a_pc_gets_the_model_its_memory_allows(config, ram, model):
    cfg = config(GGUF + f'gguf_model = "{GGUF_14}"', ram)
    assert cfg["gguf_model"] == model


def test_too_little_memory_downloads_no_note_writer(config):
    cfg = config(MLX + f'mlx_llm_model = "{MLX_14}"', 8)
    assert cfg["llm_fits"] is False
    assert [r for r, _ in ms.model_plan(cfg)] == [cfg["whisper_model"], cfg["voice_model"]]


def test_a_model_already_installed_is_left_alone(config, tmp_path):
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / "models_ready.json").write_text(json.dumps([[MLX_14, None]]))
    assert config(MLX + f'mlx_llm_model = "{MLX_14}"', 16)["mlx_llm_model"] == MLX_14


def test_the_limit_can_be_turned_off(config):
    cfg = config(MLX + f'mlx_llm_model = "{MLX_14}"\nignore_memory_limit = true', 8)
    assert cfg["mlx_llm_model"] == MLX_14 and cfg["llm_fits"] is True


def test_a_model_someone_chose_themselves_is_not_changed(config):
    cfg = config(MLX + 'mlx_llm_model = "me/my-small-model"', 8)
    assert cfg["mlx_llm_model"] == "me/my-small-model" and cfg["llm_fits"] is True


@pytest.mark.parametrize("backend", ["cloud", "ollama"])
def test_other_backends_are_not_limited(config, backend):
    assert config(f'llm_backend = "{backend}"', 4)["llm_fits"] is True


def test_the_app_says_what_the_computer_can_do(config):
    config(MLX + f'mlx_llm_model = "{MLX_14}"', 8)
    s = app.Api().setup_state()
    assert s["local_ok"] is False and s["llm_fits"] is False and s["ram_gb"] == 8


def test_publishing_covers_every_model_the_app_can_pick():
    spec = importlib.util.spec_from_file_location("publish_models", Path(ms.HERE) / "packaging" / "publish_models.py")
    publish = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publish)
    repos = {r for r, _ in publish.MODELS}
    assert {t[1] for t in ms.LLM_TIERS} <= repos
    assert {t[2].rsplit("/", 1)[0] for t in ms.LLM_TIERS} <= repos
    assert ms.DEFAULTS["voice_model"] in repos
    assert (ms.HERE / "packaging" / "publish_models.py").exists()


def test_the_shipped_list_covers_every_model_and_fits_github():
    manifest = store.load_manifest(ms.HERE / "models.json")
    for _, mlx, gguf in ms.LLM_TIERS:
        repo, name = gguf.rsplit("/", 1)
        assert store.wanted_files(manifest, mlx, None) and store.wanted_files(manifest, repo, name)
    for repo in ("mlx-community/whisper-large-v3-turbo", "dropbox-dash/faster-whisper-large-v3-turbo", ms.DEFAULTS["voice_model"]):
        assert store.wanted_files(manifest, repo, None)
    for entry in manifest.values():
        assert len(entry["revision"]) == 40
        for f in entry["files"]:
            assert len(f["sha256"]) == 64 and sum(p["size"] for p in f["parts"]) == f["size"]
            assert all(p["size"] < 2**31 for p in f["parts"])


# -- the release


class Release(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        Release.requests.append((self.path, self.headers.get("Range")))
        if Release.status != 200:
            self.send_response(Release.status)
            self.end_headers()
            return
        body = Release.files.get(self.path[1:])
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        start = int(self.headers["Range"].split("=")[1].rstrip("-")) if self.headers.get("Range") else 0
        data = body[start:]
        if Release.cut and not start:
            data = data[:Release.cut]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def part(name, data):
    return {"name": name, "size": len(data)}


@pytest.fixture
def release(tmp_path):
    big = b"weights" * 3000
    small = b'{"a": 1}'
    Release.files = {"p.000": big[:9000], "p.001": big[9000:], "c.000": small}
    Release.requests, Release.status, Release.cut = [], 200, 0
    manifest = {"org/model": {"revision": REV, "license": "mit", "files": [
        {"path": "sub/model.bin", "size": len(big), "sha256": hashlib.sha256(big).hexdigest(), "parts": [part("p.000", big[:9000]), part("p.001", big[9000:])]},
        {"path": "config.json", "size": len(small), "sha256": hashlib.sha256(small).hexdigest(), "parts": [part("c.000", small)]}]}}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Release)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield manifest, f"http://127.0.0.1:{server.server_address[1]}/", tmp_path / "hub", big
    server.shutdown()


def fetch(manifest, base, hub, filename=None):
    store.install(manifest, "org/model", filename, hub, ssl.create_default_context(), base)


def test_split_files_are_joined_and_the_offline_loaders_find_them(release):
    manifest, base, hub, big = release
    fetch(manifest, base, hub)
    from huggingface_hub import hf_hub_download, snapshot_download
    assert Path(hf_hub_download("org/model", "sub/model.bin", cache_dir=hub, local_files_only=True)).read_bytes() == big
    folder = Path(snapshot_download("org/model", cache_dir=hub, local_files_only=True))
    assert (folder / "config.json").exists() and folder.name == REV
    assert not (hub / "models--org--model" / ".parts").exists()


def test_one_file_of_a_repository_can_be_asked_for(release):
    manifest, base, hub, _ = release
    fetch(manifest, base, hub, "config.json")
    assert not (hub / "models--org--model" / "snapshots" / REV / "sub").exists()
    assert len(Release.requests) == 1


def test_a_stopped_download_continues(release):
    manifest, base, hub, big = release
    parts = hub / "models--org--model" / ".parts"
    parts.mkdir(parents=True)
    (parts / "p.000").write_bytes(big[:4000])
    fetch(manifest, base, hub)
    assert ("/p.000", "bytes=4000-") in Release.requests
    assert (hub / "models--org--model" / "snapshots" / REV / "sub" / "model.bin").read_bytes() == big


def test_a_connection_that_drops_is_retried_from_where_it_got_to(release, monkeypatch):
    manifest, base, hub, big = release
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    Release.cut = 2000
    fetch(manifest, base, hub)
    assert (hub / "models--org--model" / "snapshots" / REV / "sub" / "model.bin").read_bytes() == big


def test_a_network_that_gives_nothing_is_reported_quickly(release, monkeypatch):
    manifest, _, hub, _ = release
    naps = []
    monkeypatch.setattr(store.time, "sleep", naps.append)
    with pytest.raises(store.StoreError, match="Couldn't reach GitHub"):
        store.install(manifest, "org/model", None, hub, ssl.create_default_context(), "http://127.0.0.1:9/")
    assert len(naps) == 1


def test_a_garbled_answer_from_a_proxy_is_an_explained_failure(release, monkeypatch):
    manifest, base, hub, _ = release
    monkeypatch.setattr(store.time, "sleep", lambda s: None)

    class Garbled:
        def open(self, *a, **k):
            raise http.client.BadStatusLine("<html>blocked</html>")
    monkeypatch.setattr(store.urllib.request, "build_opener", lambda *a: Garbled())
    with pytest.raises(store.StoreError, match="BadStatusLine"):
        fetch(manifest, base, hub)


def test_broken_trust_store_falls_back_to_certificates_that_exist(monkeypatch):
    import truststore
    monkeypatch.setattr(truststore, "SSLContext", lambda *a: 1 / 0)
    assert store.context().verify_mode == ssl.CERT_REQUIRED


def test_a_finished_file_is_not_fetched_again(release):
    manifest, base, hub, _ = release
    fetch(manifest, base, hub)
    Release.requests.clear()
    fetch(manifest, base, hub)
    assert Release.requests == []


def test_tampered_content_is_thrown_away(release):
    manifest, base, hub, _ = release
    Release.files["p.001"] = b"x" * len(Release.files["p.001"])
    with pytest.raises(store.StoreError, match="damaged"):
        fetch(manifest, base, hub)
    assert not (hub / "models--org--model" / "snapshots").exists()


@pytest.mark.parametrize("path", ["../../evil.txt", "/etc/evil"])
def test_a_path_cannot_leave_the_model_folder(release, path):
    manifest, base, hub, _ = release
    manifest["org/model"]["files"][1]["path"] = path
    with pytest.raises(store.StoreError, match="damaged"):
        fetch(manifest, base, hub, path)


@pytest.mark.parametrize("status,words", [(403, "ask IT"), (407, "sign-in"), (404, "error 404")])
def test_errors_say_what_happened(release, status, words):
    manifest, base, hub, _ = release
    Release.status = status
    with pytest.raises(store.StoreError, match=words):
        fetch(manifest, base, hub)


def test_too_little_disk_is_refused_before_anything_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(store.shutil, "disk_usage", lambda p: type("U", (), {"free": 3 * 2**30}))
    with pytest.raises(store.StoreError, match="disk space"):
        store.check_space(tmp_path, 2 * 2**30)


def test_plan_size_needs_every_entry():
    m = {"a/b": {"files": [{"path": "x", "size": 5}, {"path": "y", "size": 7}]}}
    assert store.plan_size(m, [("a/b", None)]) == 12 and store.plan_size(m, [("a/b", "y")]) == 7
    assert store.plan_size(m, [("a/b", None), ("c/d", None)]) is None


# -- the download command


@pytest.fixture
def download(config, monkeypatch, tmp_path):
    cfg = config(MLX + f'mlx_llm_model = "{MLX_14}"', 32)
    plan = [list(x) for x in ms.model_plan(cfg)]
    calls = []
    manifest = {r: {"revision": REV, "files": [{"path": "f", "size": 1, "sha256": "", "parts": []}]} for r, _ in plan}
    monkeypatch.setattr(ms, "model_manifest", lambda: manifest)
    monkeypatch.setattr(ms.model_store, "check_space", lambda *a: None)
    monkeypatch.setattr(ms.model_store, "context", lambda *a: None)
    monkeypatch.setattr(ms.model_store, "install", lambda m, repo, name, hub, ctx: calls.append(("github", repo)))
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda repo, **k: calls.append(("hf", repo)))
    return cfg, calls, plan


def test_the_release_is_tried_first(download):
    cfg, calls, plan = download
    ms.cmd_download(cfg, None)
    assert calls == [("github", r) for r, _ in plan]
    assert json.loads((cfg["work_dir"] / "models_ready.json").read_text()) == plan


def test_hugging_face_is_the_fallback(download, monkeypatch):
    cfg, calls, plan = download

    def blocked(*a):
        raise ms.model_store.StoreError("nope")
    monkeypatch.setattr(ms.model_store, "install", blocked)
    ms.cmd_download(cfg, None)
    assert calls == [("hf", r) for r, _ in plan]


def test_when_both_fail_the_reason_reaches_the_app(download, monkeypatch):
    cfg, _, _ = download

    def blocked(*a, **k):
        raise ms.model_store.StoreError("This network refused the download.")
    monkeypatch.setattr(ms.model_store, "install", blocked)
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "snapshot_download", blocked)
    with pytest.raises(SystemExit):
        ms.cmd_download(cfg, None)
    assert "refused" in (cfg["work_dir"] / "download_error.txt").read_text() and not (cfg["work_dir"] / "models_ready.json").exists()
    api = app.Api()
    api.download = type("P", (), {"poll": lambda self: 1})()
    assert "refused" in api.download_progress()["error"]


def crashing(download, monkeypatch, error):
    cfg, _, _ = download

    def boom(*a, **k):
        raise error
    monkeypatch.setattr(ms.model_store, "check_space", boom)
    with pytest.raises(SystemExit):
        ms.cmd_download(cfg, None)
    api = app.Api()
    api.download = type("P", (), {"poll": lambda self: 1})()
    return api.download_progress()["error"]


@pytest.mark.parametrize("error,words", [(PermissionError("no access to .cache"), "PermissionError: no access"), (RuntimeError("odd"), "RuntimeError: odd")])
def test_an_unexpected_crash_still_says_what_happened(download, monkeypatch, error, words):
    assert words in crashing(download, monkeypatch, error)


def test_install_crashing_oddly_still_tries_hugging_face(download, monkeypatch):
    cfg, calls, plan = download

    def odd(*a):
        raise ValueError("strange")
    monkeypatch.setattr(ms.model_store, "install", odd)
    ms.cmd_download(cfg, None)
    assert calls == [("hf", r) for r, _ in plan]


def test_the_last_printed_line_is_shown_when_nothing_was_recorded(config, tmp_path):
    cfg = config()
    cfg["log_dir"].mkdir(parents=True, exist_ok=True)
    (cfg["log_dir"] / "download.log").write_text("Downloading a\nKilled: 9\n", encoding="utf-8")
    api = app.Api()
    api.download = type("P", (), {"poll": lambda self: 9})()
    assert api.download_progress()["error"] == "Killed: 9"


def test_no_record_at_all_is_still_a_clear_message(config):
    config()
    api = app.Api()
    api.download = type("P", (), {"poll": lambda self: 1})()
    assert "before it could say why" in api.download_progress()["error"]


# -- downloading by hand


def run_script(release, tmp_path):
    manifest, base, hub, _ = release
    windows = sys.platform == "win32"
    path = tmp_path / ("get.ps1" if windows else "get.sh")
    path.write_text(store.script(manifest, [("org/model", None)], str(hub), windows, base), encoding="utf-8-sig" if windows else "utf-8", newline="\n")
    return subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(path)] if windows else ["bash", str(path)], capture_output=True, text=True, timeout=120)


def test_the_hand_command_installs_what_the_app_accepts(release, tmp_path):
    manifest, base, hub, big = release
    r = run_script(release, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert store.missing(manifest, [("org/model", None)], hub) == []
    from huggingface_hub import hf_hub_download
    assert Path(hf_hub_download("org/model", "sub/model.bin", cache_dir=hub, local_files_only=True)).read_bytes() == big


def test_the_hand_command_can_be_run_again(release, tmp_path):
    manifest, _, hub, _ = release
    assert run_script(release, tmp_path).returncode == 0
    Release.requests.clear()
    assert run_script(release, tmp_path).returncode == 0
    assert Release.requests == [] and store.missing(manifest, [("org/model", None)], hub) == []


def test_the_hand_command_refuses_a_damaged_download(release, tmp_path):
    manifest, _, hub, _ = release
    Release.files["p.001"] = b"x" * len(Release.files["p.001"])
    assert run_script(release, tmp_path).returncode != 0
    assert store.missing(manifest, [("org/model", None)], hub)


def test_a_damaged_file_is_found(release):
    manifest, base, hub, _ = release
    fetch(manifest, base, hub)
    assert store.missing(manifest, [("org/model", None)], hub) == []
    target = hub / "models--org--model" / "snapshots" / REV / "config.json"
    target.write_text("{}")
    assert store.missing(manifest, [("org/model", None)], hub) == ["org/model/config.json"]


def test_a_path_with_quotes_and_spaces_is_safe_in_the_script(release):
    manifest, _, _, _ = release
    for windows in (True, False):
        text = store.script(manifest, [("org/model", None)], "/x/it's a folder; rm -rf ~", windows)
        assert "it''s a folder" in text if windows else "'/x/it'\"'\"'s a folder; rm -rf ~'" in text


def test_the_app_only_accepts_models_that_check_out(config, tmp_path, monkeypatch):
    cfg = config(MLX + f'mlx_llm_model = "{MLX_14}"', 8)
    from huggingface_hub import constants
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path / "hub"))
    api = app.Api()
    assert api.models_check()["ok"] is False and not (cfg["work_dir"] / "models_ready.json").exists()
    monkeypatch.setattr(ms.model_store, "missing", lambda *a: [])
    assert api.models_check()["ok"] is True and ms.models_ready(ms.load_config())
    assert "get-models" in api.models_script()["command"]

