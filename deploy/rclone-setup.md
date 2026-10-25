# rclone Dropbox setup (one-time)

Produces the `rclone-config/` directory that `deploy/docker-compose.yml` mounts
into the scheduler container. OAuth needs a browser, so the authorization runs
on the laptop and the resulting config is copied to the server.

Do this once, before the first Dropbox mirror.

## 1. Create a dedicated Dropbox app

rclone ships a shared client ID that is heavily rate-limited (`too_many_requests`
during a bulk mirror). Use your own.

At <https://www.dropbox.com/developers/apps> → **Create app**:

- [ ] API: **Scoped access**
- [ ] Access type: **App folder** (isolates it to `Apps/<app name>/`) or **Full
      Dropbox** — either works; App folder is the tighter choice
- [ ] Name it something identifiable, e.g. `receipt-index-mirror`

Then, **before authorizing anything**, on the app's **Permissions** tab enable:

- [ ] `account_info.read`
- [ ] `files.metadata.write`
- [ ] `files.content.write`
- [ ] `files.content.read`
- [ ] Click **Submit**

> Scopes are frozen into the token at authorize time. Enabling a scope after
> the fact does nothing until you re-run the authorization — the symptom is a
> `missing_scope` error at the first upload, not at setup.

On the **Settings** tab:

- [ ] Add OAuth 2 redirect URI: `http://localhost:53682/`
- [ ] Copy the **App key** and **App secret**

## 2. Authorize on the laptop

```bash
rclone config
```

- [ ] `n` — new remote
- [ ] name: `dropbox`  (must match the `dropbox:` prefix in `deploy/crontab`)
- [ ] storage: `dropbox`
- [ ] `client_id`: the App key from step 1
- [ ] `client_secret`: the App secret from step 1
- [ ] advanced config: `n`
- [ ] use auto config: `y` — opens a browser, redirects to `localhost:53682`
- [ ] confirm, then `q` to quit

Verify:

```bash
rclone lsd dropbox:
rclone mkdir dropbox:receipt-index/receipts
rclone mkdir dropbox:receipt-index/backups
```

## 3. Copy the config directory to the server

Copy the whole directory, not just `rclone.conf` (see step 4).

```bash
rclone config file          # prints the path, usually ~/.config/rclone/rclone.conf
scp -r ~/.config/rclone/ receipt-server:/path/to/receipt-index/rclone-config
```

On the server, in the clone root:

```bash
sudo chown -R 1000:1000 rclone-config
chmod 700 rclone-config
chmod 600 rclone-config/rclone.conf
```

uid/gid 1000 is the image's `appuser`. `rclone-config/` is git-ignored.

## 4. Why the mount is read-write

`deploy/docker-compose.yml` mounts `../rclone-config:/app/.rclone` with **no
`:ro`**, and mounts the **directory**, never `rclone.conf` alone. Both are
load-bearing:

Dropbox access tokens expire after about four hours. On refresh, rclone
rewrites its config by writing a sibling temp file and `rename()`-ing it over
`rclone.conf`. A read-only mount fails the write; a single-file bind mount
pins the inode so the rename cannot replace it. Either way the container keeps
the stale token, and the mirror stops working a few hours after a deploy that
looked fine — with the only symptom buried in the container log.

## 5. Verify from inside the container

```bash
docker compose -f deploy/docker-compose.yml exec scheduler \
    rclone lsd dropbox: --config /app/.rclone/rclone.conf
```

This must succeed **as uid 1000** — it proves both the ownership and the
credentials, which an `ls` on the host does not.

Leave the sync cron lines disabled until the initial bulk mirror has been run
manually and file counts match. `rclone copy` is additive and never deletes, so
a bad first mirror does not self-clean:

```bash
docker compose -f deploy/docker-compose.yml exec scheduler \
    rclone copy /app/data/receipts dropbox:receipt-index/receipts \
    --config /app/.rclone/rclone.conf -v

# counts must match
find data/receipts -type f | wc -l
rclone size dropbox:receipt-index/receipts
```
