# Mihomo subscription proxy

This deployment owns the loopback proxy on `127.0.0.1:7999` used by the
Pixelle service for OpenAI requests. The subscription URL is a secret and must
never be committed.

## Provision

1. Rotate the subscription token if it has been exposed.
2. Create `/etc/huangque/mihomo-new.env` as `root:root` with mode `0600`:

   ```bash
   GRAYFOX_SUBSCRIPTION_URL=https://provider.example/subscription
   ```

3. From the checked-out deployment repository, run:

   ```bash
   sudo bash deploy/mihomo-new/install.sh
   ```

The installer renders a private config, validates it with `mihomo -t`, and
only then replaces and restarts `mihomo-new.service`.

## Verify

```bash
systemctl status mihomo-new.service --no-pager
curl --proxy http://127.0.0.1:7999 --connect-timeout 15 \
  --max-time 30 --output /dev/null --write-out '%{http_code}\n' \
  https://api.openai.com/v1/models
```

An unauthenticated OpenAI check should reach the API and return `401`; a
timeout or TLS error means the proxy path is not ready. After this check, run
the authenticated Pixelle narration smoke test before considering deployment
complete.
