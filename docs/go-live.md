# Going live

Sentinel is served at **`https://sentinel.yashkumarvaibhav.me`** through the
existing Cloudflare tunnel, exactly like the owner's other apps. Everything
below the tunnel edit is agent-runnable; **the tunnel edit itself is
owner-executed** — restarting the tunnel briefly interrupts every app behind it
(placement-atlas, gwiz, tradevault, coexist, placeprep).

## Order of operations

### 1. The access gate must be armed first

The public deployment must never let an anonymous visitor act. Set a shared
secret in `.env` before exposure:

```bash
# a long random value; never commit it, never log it
echo "SENTINEL_SHARED_SECRET=$(openssl rand -hex 32)" >> .env
```

With this set, mutating requests (POST/PUT/PATCH/DELETE) and sensitive routes
(`/api/lab/*`) require the `x-sentinel-secret` header; reads stay open so the
dashboard works for anyone with the link. This is a stopgap, replaced wholesale
by OIDC + RBAC in Phase 8.1 — it answers "may this request act", not "who is
this".

### 2. The front door must be up

```bash
make up
curl -sf http://127.0.0.1:8041/api/version   # front door serves, SHA matches HEAD
```

The tunnel dials Caddy on `127.0.0.1:8041`; nothing resolves until it is up.

### 3. The tunnel rule — OWNER-EXECUTED

The owner adds one ingress rule **above** the `http_status:404` catch-all in
`~/.cloudflared/config.yml`, pointing at the front door:

```yaml
  - hostname: sentinel.yashkumarvaibhav.me
    service: http://127.0.0.1:8041
```

then the DNS route and restart:

```bash
cp ~/.cloudflared/config.yml ~/.cloudflared/config.yml.bak.$(date +%s)   # back up first
cloudflared tunnel route dns 013ccc76-8995-4190-96e8-8d3b36663a5d sentinel.yashkumarvaibhav.me
systemctl --user restart gwiz-tunnel.service   # briefly interrupts ALL tunnelled apps
```

**Change nothing else** — not existing ingress entries, not credentials, not
the catch-all. After the restart, confirm every existing hostname still answers:

```bash
for h in placement-atlas yashkumarvaibhav gwiz tradevault coexist placeprep; do
  curl -s -o /dev/null -w "$h %{http_code}\n" "https://${h}.yashkumarvaibhav.me/" 2>/dev/null || \
  curl -s -o /dev/null -w "root %{http_code}\n" "https://yashkumarvaibhav.me/"
done
curl -s -o /dev/null -w "sentinel %{http_code}\n" https://sentinel.yashkumarvaibhav.me/api/version
```

## Running as a service

The compose stack runs under a **user** systemd unit
(`deploy/systemd/sentinel.service`), matching the other apps on this host.
Lingering is already enabled, so it survives logout.

```bash
cp deploy/systemd/sentinel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sentinel.service
systemctl --user status sentinel.service
```

The testbed cluster is **not** managed by this unit — it is started on demand
(`make lab-up`) for scoring and torn down after, because it is heavy and this
host is shared.

## Redeploying

The public URL always serves the latest working `main`:

```bash
git pull
make up            # --build rebuilds changed images; --wait holds for health
curl -s http://127.0.0.1:8041/api/version   # footer/version SHA == git rev-parse --short HEAD
```
