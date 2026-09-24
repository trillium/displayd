# Webhook self-deploy runbook: push-to-main redeploys the lnx panel

Path of a push: GitHub → `https://vps01.hippo-tilapia.ts.net/hooks/displayd`
(Tailscale funnel on vps01) → forwarder on vps01 `127.0.0.1:8090` →
receiver on lnx `100.81.88.113:9898` → local deploy → panel `:8980`.

## Why this shape

- **Code home is `hooks/` in the displayd repo** (not standalone): deploy.sh's
  rsync ships `hooks/` to `~/displayd` on every deploy, so the receiver
  travels with the code it deploys. The one exception is the vps01
  forwarder copy at `/root/displayd-funnel/` -- vps01 holds no repo
  checkout, so its file is copied there by hand (source of truth stays
  here).
- **Receiver on lnx, forwarder on vps01** (not receiver-on-vps01 with SSH
  into lnx): vps01 has no git and no SSH path to lnx, while lnx already
  has GitHub-over-HTTPS, passwordless sudo for the daemon restart, and
  the panel API next door. The funnel config (`tailscale serve` →
  `127.0.0.1:8090`) is untouched; only the process behind `:8090`
  changed from the proof logger to the forwarder.
- **Receiver is a separate process from the daemon** (not a route in
  `displayd.py`): the deploy restarts the daemon, which would kill an
  in-flight deploy if it lived inside it.

## Layout

| Where | What |
|---|---|
| lnx `~/displayd/hooks/webhook_receiver.py` | receiver (stdlib only), systemd `displayd-webhook.service` |
| lnx `~/displayd-upstream` | throwaway git clone, redeployed from on each push |
| lnx `/root/displayd-webhook-secret` | shared HMAC secret, `0600 root:root` (never in repo/logs) |
| lnx `/var/log/displayd-webhook.log` | JSONL deploy log |
| vps01 `/root/displayd-funnel/funnel_forwarder.py` | forwarder (stdlib only), systemd `displayd-funnel-fwd.service` |
| vps01 `/root/funnel-proof/hook.py` | RETIRED proof logger, kept for reference/restore |
| GitHub `trillium/displayd` settings → Webhooks | push events, JSON, secret; branch filter is in the receiver (`refs/heads/main` only) since GitHub hooks cannot filter branches |

## Install / reinstall (from a checkout)

```sh
# 1. ship the code (deploy.sh rsync covers hooks/ automatically)
./deploy.sh
# ...or surgically: rsync -az hooks/ trillium@lnx-server:~/displayd/hooks/

# 2. lnx: upstream clone (once)
ssh trillium@lnx-server 'sudo -u trillium git clone https://github.com/trillium/displayd /home/trillium/displayd-upstream'
# (already exists after first install; receiver reuses it)

# 3. lnx: secret (once; generate on the host so it never crosses a terminal)
ssh trillium@lnx-server "python3 -c 'import secrets; open(\"/tmp/whsec\",\"w\").write(secrets.token_hex(32))" \
  '&& sudo install -m 600 -o root -g root /tmp/whsec /root/displayd-webhook-secret && rm /tmp/whsec'

# 4. lnx: unit
ssh trillium@lnx-server 'sudo cp ~/displayd/hooks/displayd-webhook.service /etc/systemd/system/ \
  && sudo systemctl daemon-reload && sudo systemctl enable --now displayd-webhook'

# 5. vps01: forwarder files + unit
scp hooks/funnel_forwarder.py root@vps01:/root/displayd-funnel/
scp hooks/displayd-funnel-fwd.service root@vps01:/etc/systemd/system/
ssh root@vps01 'systemctl daemon-reload && systemctl enable --now displayd-funnel-fwd'

# 6. GitHub hook (push events; secret from a file, never argv):
python3 - <<'EOF'
import json, subprocess
sec = open('/tmp/whsec-local').read().strip()  # 0600, shredded after
body = {"name": "web", "active": True, "events": ["push"],
        "config": {"url": "https://vps01.hippo-tilapia.ts.net/hooks/displayd",
                   "content_type": "json", "secret": sec}}
p = subprocess.run(["gh", "api", "repos/trillium/displayd/hooks",
                    "--input", "-"], input=json.dumps(body).encode(),
                   capture_output=True)
print(p.returncode, p.stdout.decode()[:200], p.stderr.decode()[:200])
EOF
```

## Verify

```sh
curl -s http://100.81.88.113:9898/health            # receiver alive + last deploy
curl -s http://100.81.88.113:8980/deploy             # panel stamp (sha should move on push)
ssh root@vps01 'systemctl is-active displayd-funnel-fwd'
ssh trillium@lnx-server 'sudo systemctl is-active displayd-webhook; tail -3 /var/log/displayd-webhook.log'
```

## Restore the old proof path (until green, and any time after)

```sh
ssh root@vps01 'systemctl stop displayd-funnel-fwd; nohup python3 /root/funnel-proof/hook.py >/var/log/funnel-proof.log 2>&1 &'
# funnel target :8090 is the proof logger again; nothing else changes.
```

## Secret rotation

Generate a new secret on lnx (step 3 above), update the GitHub hook
(`gh api repos/trillium/displayd/hooks/<id> -X PATCH --input -`), then
`sudo systemctl restart displayd-webhook`. The receiver reads the secret
once at startup.
