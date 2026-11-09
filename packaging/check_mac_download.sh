#!/bin/bash
# The packaged Mac app's model download, in the ways it can go wrong: failures must explain
# themselves, and the command a person can run by hand must leave a working install.
#   packaging/check_mac_download.sh dist/MeetingScribe.app/Contents/MacOS/MeetingScribe
set -u
APP="$1"
T="$(mktemp -d)"
mkdir -p "$T/w" "$T/n" "$T/logs"
cat > "$T/c.toml" <<EOF
notify = false
work_dir = "$T/w"
output_dir = "$T/n"
log_dir = "$T/logs"
ignore_memory_limit = true
mlx_llm_model = "mlx-community/Qwen2.5-7B-Instruct-4bit"
EOF
export MEETINGSCRIBE_CONFIG="$T/c.toml" HF_HUB_CACHE="$T/hub"
fail() { echo "FAILED: $1"; exit 1; }

echo "== a network that blocks everything"
sudo cp /etc/hosts /tmp/hosts.saved
for h in github.com release-assets.githubusercontent.com objects.githubusercontent.com huggingface.co; do
  echo "127.0.0.1 $h" | sudo tee -a /etc/hosts > /dev/null
done
sudo dscacheutil -flushcache
"$APP" download-models; code=$?
sudo cp /tmp/hosts.saved /etc/hosts
sudo dscacheutil -flushcache
[ "$code" -ne 0 ] || fail "a blocked download reported success"
[ -s "$T/w/download_error.txt" ] || fail "a blocked download recorded no reason"
echo "reason: $(cat "$T/w/download_error.txt")"
grep -q "ask IT" "$T/w/download_error.txt" || fail "the reason does not say what to do"

echo "== a model folder that cannot be written"
HF_HUB_CACHE=/var/root/nope/hub "$APP" download-models; code=$?
[ "$code" -ne 0 ] || fail "an unwritable folder reported success"
echo "reason: $(cat "$T/w/download_error.txt")"
grep -q "PermissionError" "$T/w/download_error.txt" || fail "the crash was not explained"

echo "== downloading by hand"
"$APP" models-script || fail "no script"
[ -s "$T/w/get-models.sh" ] || fail "script was not written"
"$APP" check-models && fail "checked out with nothing downloaded"
[ ! -f "$T/w/models_ready.json" ] || fail "marked ready with nothing downloaded"
bash "$T/w/get-models.sh" || fail "the hand download failed"
"$APP" check-models || fail "the hand download did not check out"
[ -s "$T/w/models_ready.json" ] || fail "not marked ready"

echo "== a damaged file is caught and repaired"
target="$(find "$T/hub" -name config.json | head -1)"
echo damage >> "$target"
"$APP" check-models && fail "a damaged file passed"
bash "$T/w/get-models.sh" || fail "the repair failed"
"$APP" check-models || fail "the repair did not check out"
echo "all good"
