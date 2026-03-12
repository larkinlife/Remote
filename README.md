# Firebase Alarm Mesh

Cross-waking system for Firebase Studio workspaces. Keeps N machines alive through full-mesh heartbeats, automatic wake-ups, and survivor mode.

## How it works

Each machine runs `alarm_mesh.py` which:
- Pings all other machines every 30-90 seconds
- Detects failures after 5 consecutive missed heartbeats
- Wakes downed machines via Google Cloud OAuth + workspace URL visit
- Switches to aggressive 5-min wake cycles if 2+ peers are down (survivor mode)
- Coordinates wake-ups via shared visit ledger (avoids duplicates)

## Quick start

### New workspace from this template
1. Create workspace in Firebase Studio from this repo
2. Wait ~30s for tmate to start
3. Get SSH: `https://8080-<workspace>.cloudworkstations.dev/links`

### Add machine to mesh
```bash
cd local/
python3 add_machine.py SSH_TOKEN
```

### Deploy / update
```bash
cd local/
python3 deploy.py ALL          # Full deploy to all machines
python3 deploy.py --config     # Just update machines.json
```

### Check status
```bash
cd local/
bash connect.sh status         # All machines
bash connect.sh A              # Connect to machine A
```

## Full setup guide

See [SETUP.md](SETUP.md) for detailed instructions.
