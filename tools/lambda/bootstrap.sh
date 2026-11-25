#!/usr/bin/env bash
# =============================================================================
# Anonymous V2 - OrcaSAQ-2-Cyber-27B bootstrap for a Lambda Cloud on-demand VM
# (gpu_1x_a6000, 48 GB VRAM, 14 vCPU, 100 GiB RAM, 512 GiB SSD, $1.09/h).
#
# Runs on Lambda's default image (Ubuntu 22.04 + NVIDIA driver + Lambda Stack)
# as root, either from cloud-init user-data or interactively:
#     scp tools/lambda/bootstrap.sh ubuntu@<ip>:/tmp/
#     ssh ubuntu@<ip> 'sudo bash /tmp/bootstrap.sh'
#
# Deltas vs tools/vultr/bootstrap.sh - everything else is the same logic:
#   * NO ufw block. Lambda filters inbound traffic at the edge (the account's
#     global firewall ruleset, default: tcp/22 from anywhere). Enabling ufw
#     inside the VM would lock SSH out the moment the operator IP changes and
#     Lambda has no out-of-band console to undo it. provision_orcasaq.ps1 adds
#     "tcp/8080 from <operator CIDR>" to the edge ruleset instead, so the proxy
#     is still unreachable from any other source.
#   * The login user is "ubuntu" (passwordless sudo); cloud-init still runs this
#     as root, so the script itself does not care which one launched it.
#   * Billing is per minute and ONLY the terminate API stops it. `sudo shutdown`
#     leaves the instance in "unhealthy" and the meter keeps running - use
#     provision_orcasaq.ps1 -Action Destroy.
#
# It is idempotent - every step checks before it acts, so a re-run after a
# failure costs seconds instead of re-doing the 15 GB download.
#
# What it produces:
#   * Ollama listening on 127.0.0.1:11434 (never the public interface)
#   * model id ${MODEL_ID}, registered from the Hugging Face GGUF
#   * nginx on 0.0.0.0:${PROXY_PORT} that enforces a bearer token and proxies
#     to Ollama, so the OpenAI surface is http://<ip>:${PROXY_PORT}/v1
#   * the edge firewall rule (added at launch) restricts tcp/${PROXY_PORT} to
#     the operator CIDR; the guest firewall is left alone on purpose
#   * /opt/av2/READY, /opt/av2/api.token - read these for the report
#
# Fallback: if Ollama's runner rejects the architecture (Qwen3.5 hybrid
# linear/full attention is new), the script builds llama.cpp with CUDA and
# serves the same GGUF through llama-server on the same port.  AV2 needs no
# change: both expose an OpenAI-compatible /v1 with a bearer token.
# =============================================================================
set -euo pipefail

MODEL_REPO="${MODEL_REPO:-isilicon/mango-orcasaq2-cyber-27b}"
MODEL_FILE="${MODEL_FILE:-OrcaSAQ-2-27B-Uncensored.gguf}"
#: Canonical repo is orcarouter/OrcaSAQ-2-Cyber-27B-Uncensored-GGUF but it is
#: gated (gated=auto -> GatedRepo 401 without an account that accepted the
#: terms).  The mirror above carries the same file, same byte size, ungated.
#: Export HF_TOKEN and MODEL_REPO=<canonical> to use the original instead.
HF_TOKEN="${HF_TOKEN:-}"
EXPECTED_BYTES="${EXPECTED_BYTES:-15676553472}"
MODEL_ID="${MODEL_ID:-orcasaq-cyber:27b}"
NUM_CTX="${NUM_CTX:-32768}"
PARALLEL="${PARALLEL:-4}"
PROXY_PORT="${PROXY_PORT:-8080}"
EDGE_CIDR="${EDGE_CIDR:-__EDGE_CIDR__}"

OPT=/opt/av2
LOG=/var/log/av2-bootstrap.log
mkdir -p "$OPT"
touch "$LOG"

log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG"; }
die() { log "FATAL: $*"; exit 1; }

log "=== AV2 bootstrap start (host=$(hostname)) ==="
. /etc/os-release 2>/dev/null || true
log "os=${PRETTY_NAME:-unknown} kernel=$(uname -r)"

# ---------------------------------------------------------------------------
# 1. GPU sanity.  Lambda's default image ships the driver and Lambda Stack; if
#    it is missing we install the server driver and tell the operator a reboot
#    is required (Lambda has no reboot-free driver load).
# ---------------------------------------------------------------------------
if command -v nvidia-smi >/dev/null 2>&1; then
  log "nvidia-smi: $(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader | head -1)"
else
  log "nvidia-smi missing - installing the NVIDIA server driver"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ubuntu-drivers-common >/dev/null
  DEBIAN_FRONTEND=noninteractive ubuntu-drivers install --gpgpu || true
  log "driver install finished; a reboot may be required before inference works"
fi

# ---------------------------------------------------------------------------
# 2. Base packages
# ---------------------------------------------------------------------------
if [ ! -f "$OPT/.packages" ]; then
  log "installing base packages"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    curl ca-certificates jq nginx openssl pciutils >/dev/null
  touch "$OPT/.packages"
fi

# ---------------------------------------------------------------------------
# 3. Ollama, loopback only, tuned for one big model on one GPU
# ---------------------------------------------------------------------------
if ! command -v ollama >/dev/null 2>&1; then
  log "installing Ollama"
  curl -fsSL https://ollama.com/install.sh | sh >>"$LOG" 2>&1 || die "ollama install failed"
else
  log "Ollama present: $(ollama --version 2>&1 | head -1)"
fi

mkdir -p /etc/systemd/system/ollama.service.d
cat >/etc/systemd/system/ollama.service.d/override.conf <<EOF
[Service]
# Loopback only: nginx terminates the public surface and checks the token.
Environment="OLLAMA_HOST=127.0.0.1:11434"
# Keep the 15 GB model resident between AV2 turns; reloading costs ~20 s.
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_NUM_PARALLEL=${PARALLEL}"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
EOF
systemctl daemon-reload
systemctl enable ollama >/dev/null 2>&1 || true
systemctl restart ollama
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 || die "ollama daemon did not come up"
log "ollama daemon is up on 127.0.0.1:11434"

# ---------------------------------------------------------------------------
# 4. Fetch the GGUF once, then register it with Ollama from the local file.
#
#    Downloading with curl instead of letting Ollama resolve the Hub URL is
#    deliberate: it is resumable, it is the same artefact the llama.cpp
#    fallback needs, and it removes a whole class of "runner could not parse
#    the FROM line" failures from the critical path.
# ---------------------------------------------------------------------------
GGUF="$OPT/model.gguf"
HAVE=$(stat -c%s "$GGUF" 2>/dev/null || echo 0)
if [ "$HAVE" -ne "$EXPECTED_BYTES" ]; then
  log "downloading ${MODEL_FILE} (~15 GB, have ${HAVE} of ${EXPECTED_BYTES} bytes)"
  AUTH=()
  if [ -n "$HF_TOKEN" ]; then AUTH=(-H "Authorization: Bearer ${HF_TOKEN}"); fi
  curl -fL --retry 5 --retry-delay 5 -C - "${AUTH[@]}" -o "$GGUF" \
    "https://huggingface.co/${MODEL_REPO}/resolve/main/${MODEL_FILE}" >>"$LOG" 2>&1 \
    || die "GGUF download failed (gated repo? export HF_TOKEN=<read token> and MODEL_REPO=orcarouter/OrcaSAQ-2-Cyber-27B-Uncensored-GGUF)"
  HAVE=$(stat -c%s "$GGUF" 2>/dev/null || echo 0)
  [ "$HAVE" -eq "$EXPECTED_BYTES" ] || die "GGUF size mismatch: ${HAVE} != ${EXPECTED_BYTES}"
fi
# A gated/404 response body saved next to a resumed download yields a file of
# the right length that is NOT a GGUF - the header is the cheap proof.
[ "$(head -c 4 "$GGUF")" = "GGUF" ] || die "$GGUF does not start with the GGUF magic - delete it and re-run"
log "GGUF verified: $(du -h "$GGUF" | cut -f1) (${EXPECTED_BYTES} bytes, magic ok)"

cat >"$OPT/Modelfile" <<EOF
FROM $GGUF
PARAMETER num_ctx ${NUM_CTX}
PARAMETER temperature 0.6
PARAMETER top_p 0.95
PARAMETER repeat_penalty 1.05
EOF

BACKEND=ollama
if ollama list 2>/dev/null | grep -q "${MODEL_ID%%:*}"; then
  log "model ${MODEL_ID} already registered"
else
  log "registering ${MODEL_ID} from the local GGUF"
  if ! ollama create "$MODEL_ID" -f "$OPT/Modelfile" >>"$LOG" 2>&1; then
    log "ollama create FAILED - tail follows"
    tail -n 25 "$LOG" | tee -a "$LOG"
    BACKEND=llamacpp
  fi
fi

echo "$BACKEND" >"$OPT/backend"
log "inference backend: $BACKEND"

# ---------------------------------------------------------------------------
# 5. Fallback: llama.cpp with CUDA, same GGUF, same OpenAI surface.
#    Only taken when Ollama's runner rejects the architecture.
# ---------------------------------------------------------------------------
if [ "$BACKEND" = "llamacpp" ]; then
  log "building llama.cpp (CUDA) - this takes 15-25 min on 14 vCPU"
  if [ ! -x /opt/llama.cpp/build/bin/llama-server ]; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      build-essential cmake git libcurl4-openssl-dev >/dev/null
    rm -rf /opt/llama.cpp
    git clone --depth 1 https://github.com/ggml-org/llama.cpp /opt/llama.cpp >>"$LOG" 2>&1
    cmake -S /opt/llama.cpp -B /opt/llama.cpp/build \
      -DGGML_CUDA=ON -DLLAMA_CURL=ON -DCMAKE_BUILD_TYPE=Release >>"$LOG" 2>&1
    cmake --build /opt/llama.cpp/build -j "$(nproc)" --target llama-server >>"$LOG" 2>&1 \
      || die "llama.cpp build failed"
  fi
  if [ ! -s "$GGUF" ]; then
    die "GGUF missing for the llama.cpp fallback - re-run after the download succeeds"
  fi
  TOKEN="$(cat "$OPT/api.token" 2>/dev/null || openssl rand -hex 24 | tee "$OPT/api.token")"
  cat >/etc/systemd/system/av2-llama.service <<EOF
[Unit]
Description=AV2 llama-server (OrcaSAQ-2 Cyber 27B)
After=network-online.target
[Service]
ExecStart=/opt/llama.cpp/build/bin/llama-server --model $OPT/model.gguf \\
  --host 127.0.0.1 --port 11434 --api-key $TOKEN \\
  --ctx-size $((NUM_CTX * PARALLEL)) --parallel $PARALLEL \\
  --n-gpu-layers 999 --flash-attn on --jinja --alias ${MODEL_ID}
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now av2-llama
  # Ollama must not race llama-server for 127.0.0.1:11434 on the next boot.
  systemctl stop ollama || true
  systemctl disable ollama || true
  log "llama-server started on 127.0.0.1:11434 (ollama disabled)"
fi

# ---------------------------------------------------------------------------
# 6. Public surface: nginx + bearer token.  AV2 sends
#    "Authorization: Bearer <token>" because AV2_LLM_API_KEY is set.
# ---------------------------------------------------------------------------
TOKEN="$(cat "$OPT/api.token" 2>/dev/null || openssl rand -hex 24 | tee "$OPT/api.token")"
chmod 600 "$OPT/api.token"

cat >/etc/nginx/sites-available/av2-llm <<EOF
server {
    listen ${PROXY_PORT} default_server;
    server_name _;
    client_max_body_size 32m;
    default_type application/json;
    access_log /var/log/nginx/av2-llm.access.log;
    error_log  /var/log/nginx/av2-llm.error.log warn;

    location / {
        set \$ok 0;
        if (\$http_authorization = "Bearer ${TOKEN}") { set \$ok 1; }
        if (\$ok = 0) { return 401 '{"error":{"message":"unauthorized","type":"invalid_request_error"}}'; }
        proxy_pass http://127.0.0.1:11434;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/av2-llm /etc/nginx/sites-enabled/av2-llm
rm -f /etc/nginx/sites-enabled/default
nginx -t >>"$LOG" 2>&1 || die "nginx config invalid"
systemctl enable nginx >/dev/null 2>&1 || true
systemctl restart nginx
log "nginx listening on :${PROXY_PORT} (bearer token enforced)"

# ---------------------------------------------------------------------------
# 7. Firewall: nothing to do in-guest on Lambda.
#
#    Lambda has no security groups on the VPS itself; inbound is filtered by the
#    account's global firewall ruleset at the network edge (default: tcp/22 from
#    0.0.0.0/0). provision_orcasaq.ps1 -Action Create adds
#    "tcp/${PROXY_PORT} from ${EDGE_CIDR}" there. We deliberately do NOT enable
#    ufw: a wrong/rotated operator IP would lock out SSH permanently, and there
#    is no console to fix it from.
# ---------------------------------------------------------------------------
if command -v ufw >/dev/null 2>&1; then
  if ufw status 2>/dev/null | grep -q '^Status: active'; then
    log "WARNING: ufw is active on this image - it can lock out SSH if the operator IP changes; run 'sudo ufw disable' if you did not enable it"
  else
    log "ufw present but inactive - edge ruleset is the only inbound filter"
  fi
else
  log "ufw not installed - edge ruleset is the only inbound filter"
fi
log "edge firewall: tcp/${PROXY_PORT} must be restricted to ${EDGE_CIDR} (set at launch)"

# ---------------------------------------------------------------------------
# 8. Smoke test through the public surface, exactly as AV2 will call it.
# ---------------------------------------------------------------------------
SMOKE='{"error":"not run"}'
MODELS_PATH=/v1/models
for _ in $(seq 1 30); do
  SMOKE="$(curl -sS -m 20 -H "Authorization: Bearer $TOKEN" \
    "http://127.0.0.1:${PROXY_PORT}${MODELS_PATH}" || true)"
  if printf '%s' "$SMOKE" | grep -q '"id"'; then break; fi
  sleep 5
done
printf '%s\n' "$SMOKE" >"$OPT/models.json"
log "GET ${MODELS_PATH} -> $(printf '%s' "$SMOKE" | head -c 300)"

{
  echo "backend=$BACKEND"
  echo "model_id=$MODEL_ID"
  echo "model_repo=$MODEL_REPO"
  echo "base_url=http://$(curl -sS -m 10 https://ifconfig.me/ip || hostname -I | awk '{print $1}'):${PROXY_PORT}/v1"
  echo "api_token=$TOKEN"
  echo "num_ctx=$NUM_CTX"
  echo "parallel=$PARALLEL"
  echo "operator_cidr=${EDGE_CIDR}"
  echo "edge_firewall=tcp/${PROXY_PORT} from ${EDGE_CIDR}"
  echo "ready_utc=$(date -u +%FT%TZ)"
} >"$OPT/READY"

log "=== AV2 bootstrap done ==="
cat "$OPT/READY" | tee -a "$LOG"
