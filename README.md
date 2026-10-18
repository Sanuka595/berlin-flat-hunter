# Berlin Flat Hunter

Autonomous flat monitor and notification service for municipal housing companies in Berlin (WBM, Gewobag, Gesobau, Howoge, Degewo, Stadt und Land, Berlinovo, inBerlinWohnen) featuring multi-profile search criteria, strict keyword filtering, and WBM Auto-Apply integration.

## Key Features

- **Asynchronous Scraping**: Queries municipal housing platforms via direct JSON APIs and HTML listings concurrently using `httpx`.
- **Multi-Profile Dispatch (`profiles.yaml`)**:
  - Independent criteria per profile (rent caps, room count, surface area, included/excluded districts).
  - Separate Telegram channels and webhooks per search profile.
  - Strict separation of secrets: credentials reside exclusively in `.env`, referenced by environment variable placeholders in YAML.
- **Strict Relevance Filters**:
  - **Senior / Assisted Living**: Filters out housing restricted by age (e.g., 55+, 60+, 65+), `betreutes wohnen`, `ruhestand`, and special senior WBS requirements across titles and descriptions.
  - **Students & Flatshares (WG)**: Rejects dorms, student apartments, and shared rooms unless explicitly permitted by `allow_student: true`.
  - **WBS Classification**: Detects required income vouchers (WBS 100 vs. WBS 220) and avoids false positives when listings state `ohne WBS` or `WBS erforderlich: Nein`.
- **WBM Auto-Apply (`appliers/wbm.py`)**:
  - Extracts Powermail CSRF tokens, hidden form properties, and honeypots.
  - Populates applicant information and submits contact requests automatically upon match.
  - Sends immediate Telegram confirmation with submitted payload logs.
  - Supports `dry_run` mode for verification without real form submission.
- **Persistence & Deduplication**:
  - SQLite backend (`flats.db`, `profile_seen` table) running in WAL mode with connection reuse per scraping cycle.
- **Autonomous & Daemon Ready**:
  - Continuous loop mode or single-shot execution (`--once`) returning JSON execution metrics for cron jobs or external orchestrators.
  - Graceful shutdown handling (`SIGINT`, `SIGTERM`) ensuring proper teardown of database connections and HTTP sessions.

## Installation

```bash
git clone <REPO_URL>
cd berlin-flat-hunter

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configuration

1. Create a `.env` file based on the example:
   ```bash
   cp .env.example .env
   ```
2. Populate tokens and chat IDs in `.env`:
   ```dotenv
   TELEGRAM_BOT_TOKEN_PRIMARY=123456789:ABC...
   TELEGRAM_CHAT_ID_PRIMARY=123456789
   ASSISTANT_WEBHOOK_URL=https://...
   ASSISTANT_WEBHOOK_AUTH=Bearer ...
   CHECK_INTERVAL_SECONDS=90
   ```
3. Configure search criteria in `profiles.yaml`:
   ```yaml
   profiles:
     - id: example
       display_name: Example Profile
       enabled: true
       allow_senior: false
       allow_student: false
       sources: [wbm, gewobag, gesobau, howoge, degewo, stadtundland, berlinovo, inberlinwohnen]
       rooms_min: 1.0
       rooms_max: 2.0
       warm_rent_max: 650.0
       cold_rent_max: 500.0
       wbs_required_ok: true
       wbs_categories_ok: [100]
       auto_apply: false
       telegram_chat_id: "${TELEGRAM_CHAT_ID_EXAMPLE}"
       telegram_bot_token: "${TELEGRAM_BOT_TOKEN_EXAMPLE}"
   ```

## Usage

### Continuous Monitoring
```bash
python flat_monitor.py
```

### Single Execution (Cron / Automation)
```bash
python flat_monitor.py --once
```

### Running Tests
```bash
python -m unittest -v test_profiles.py
```

### systemd User Service

To run as a background service:
```bash
mkdir -p ~/.config/systemd/user
cp flat-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now flat-monitor.service
```

Check service logs:
```bash
journalctl --user -u flat-monitor.service -f
```
