#!/usr/bin/env bash
# À lancer une fois sur le serveur (Ubuntu 24.04) : Docker, 4 Go de swap, pare-feu.
set -euo pipefail

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi

# Swap : le modèle bge-m3 et l'agent tiennent dans 4 Go de RAM, le swap évite un arrêt
# brutal en cas de pic.
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 4G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

echo "Serveur prêt. Déconnecte-toi puis reconnecte-toi pour utiliser docker sans sudo."
