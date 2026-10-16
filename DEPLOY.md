# Deploying messenger-bot on a VM

Two containers on the VM, both Docker Compose:

| Container | Compose file | Port | Purpose |
|---|---|---|---|
| `webtop` | `deploy/desktop/docker-compose.yml` | 3001 (https) | A web desktop. Its X display `:1` is exported to `/srv/x11-shared`. |
| `messenger-bot` | `docker-compose.yml` | 4300 | The console. Headed: its Chrome windows open on that display. |

The console runs the git checkout (bind-mounted), so **a deploy is `git pull`
and a restart**. Runtime state stays in `backend/data/` on the VM disk.

## First install

```bash
# 1. the shared display socket directory
sudo mkdir -p /srv/x11-shared && sudo chmod 1777 /srv/x11-shared

# 2. the code
git clone https://github.com/shahjhanconsforc-alt/FbMessenger-campaign-bot-.git fbbot
cd fbbot
cp backend/.env.example backend/.env       # set MINIO_PASSWORD (+ anything else)

# 3. the web desktop, then the console
(cd deploy/desktop && docker compose up -d)
docker compose up -d --build
```

Open the desktop at `https://<vm>:3001` and the console at `http://<vm>:4300`.
When the console opens an account, its Chrome window appears on the desktop;
log in there by hand the first time, and the profile remembers it.

## Deploy an update

```bash
cd ~/fbbot && git pull && docker compose restart
```

Rebuild only when `requirements.txt` or the Dockerfile changed:
`docker compose up -d --build`.

## Headless instead

Set `XBOT_HEADLESS=1` in `backend/.env` and restart. No desktop is needed then,
but a login that asks for a code from an email cannot be completed.

## KVM host port forwards

On the KVM host the guest is on the libvirt NAT network, so two host ports are
forwarded in: `deploy/host/fbbot-dnat.sh` + `fbbot-dnat.service`
(`<host>:30502` → console, `<host>:30903` → web desktop). Install once:

```bash
sudo cp deploy/host/fbbot-dnat.sh /usr/local/bin/ && sudo chmod +x /usr/local/bin/fbbot-dnat.sh
sudo cp deploy/host/fbbot-dnat.service /etc/systemd/system/
sudo systemctl enable --now fbbot-dnat.service
```

## Resources

Chrome is the heavy part. Give the VM at least 2 GB of RAM for one account's
window plus the desktop; add roughly 300–400 MB per extra open account.
`shm_size` is raised for both containers because Chrome crashes on Docker's
default 64 MB.

## Troubleshooting

- **"waiting for display :1"** in the console log: the desktop container is
  not up, or `/srv/x11-shared` is not mounted into both containers.
- **Chrome fails to start with "sandboxing failed"**: the console runs as root
  in the container and adds `--no-sandbox` automatically; this means the image
  is not the one from this repo's Dockerfile.
- **Profiles from a newer Chrome will not open**: the image installs Google
  Chrome stable so desktop-made profiles match; rebuild the image to update it.
