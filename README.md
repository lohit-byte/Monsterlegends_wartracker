# Monsterlegends_wartracker
# ⚔️ Monster Legends War Bot

A Discord bot designed for Monster Legends team wars. It tracks defence/attack phases, pings missing attackers, manages absences, tracks warnings, and automatically handles player aliases.

## Features
- **War Tracking**: Automatically pings for Defence Start, Attack Start, and 12h/6h/3h/1h missing attack warnings.
- **Smart Missing List**: Players just type `attacks done` to remove themselves. Staff can manually add/remove.
- **Absence Requests**: Players can request time off, which staff can approve via reactions.
- **Warning System**: Track missed attacks and send warnings via DM.
- **Alias System**: Type `!war missing <name>` instead of pinging them. Auto-cleans aliases when players leave.
- **Auto-Sync & Failover (Optional)**: Can sync state and code between two servers via a Discord channel.

## Setup Instructions

### Prerequisites
- Python 3.10+
- A Discord Bot Token

### Installation
1. **Clone the repo:**
   ```bash
   git clone https://github.com/lohit-byte/Monsterlegends_wartracker.git
   cd MonsterLegends_wartracker
