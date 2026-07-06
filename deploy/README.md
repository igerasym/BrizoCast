# Raspberry Pi Deployment

## First-time setup

```bash
# Clone the repo
cd /opt
sudo git clone git@github.com:igerasym/BrizoCast.git brizocast
sudo chown -R pi:pi brizocast
cd brizocast

# Create .env from the example
cp .env.example .env
nano .env  # Fill in TELEGRAM_BOT_TOKEN, AI_API_KEY, ADMIN_PASSWORD

# Build and start
docker compose up -d --build

# Verify
docker compose logs -f
```

## Auto-deploy (weekly)

```bash
# Install the systemd timer
sudo cp deploy/brizocast-deploy.service /etc/systemd/system/
sudo cp deploy/brizocast-deploy.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now brizocast-deploy.timer

# Check timer status
systemctl list-timers brizocast-deploy.timer

# Manual deploy
./deploy.sh
```

## Manual operations

```bash
# View logs
docker compose logs -f --tail 100
docker compose logs brizocast --tail 50    # bot only
docker compose logs brizocast-admin --tail 50  # admin only

# Restart
docker compose restart

# Stop
docker compose down

# Full rebuild (after major changes)
docker compose down
docker compose build --no-cache
docker compose up -d
```

## Accessing the admin panel

The admin panel is available at `http://<raspberry-pi-ip>:8080`.
Set `ADMIN_BIND_HOST` in `.env` to your Pi's LAN IP (e.g. `192.168.1.50`).
