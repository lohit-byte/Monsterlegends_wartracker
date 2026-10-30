# MonsterLegends_wartracker
# ⚔️ Discord War Bot

A highly customizable Discord bot for managing team wars. It tracks defence/attack phases, pings missing attackers, manages absences, tracks warnings, and automatically handles player aliases.

## Features
- **War Tracking**: Automatically pings for Defence Start, Attack Start, and 12h/6h/3h/1h missing attack warnings.
- **Dynamic Teams**: Supports as many teams as you want (configured via `config.json`).
- **Smart Missing List**: Players just type `attacks done` to remove themselves. Staff can manually add/remove.
- **Absence Requests**: Players can request time off, which staff can approve via reactions.
- **Warning System**: Track missed attacks and send warnings via DM.
- **Alias System**: Type `!war missing queen` instead of pinging them. Auto-cleans aliases when players leave.

## Setup Instructions

### 1. Prerequisites
- Python 3.10+
- A Discord Bot Token (from the Discord Developer Portal)
- **Intents Enabled**: `Message Content Intent`, `Server Members Intent`, and `Reaction Intent` must be enabled for your bot in the Discord Developer Portal.

### 2. Installation
1. Clone the repository:
   ```bash
   git clone https://github.com/lohit-byte/Monsterlegends_wartracker.git
   cd Monsterlegends_wartracker
   pip install -r requirements.txt
   cp config.example.json config.json
   python main.py

