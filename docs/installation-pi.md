# Installation du Raspberry Pi 5

Raspberry Pi OS (Bookworm ou plus récent, 64 bits). Le Pi joue trois rôles :
point d'accès Wi-Fi, serveur d'heure (NTP) pour la caméra, serveur du chrono.

## 1. Point d'accès Wi-Fi

NetworkManager crée un réseau « partagé » en 10.42.0.0/24, le Pi prend 10.42.0.1 :

```bash
sudo nmcli dev wifi hotspot ifname wlan0 con-name arrivee \
     ssid arrivee-automnales password 'un-mot-de-passe-long-et-unique'
sudo nmcli con modify arrivee connection.autoconnect yes 802-11-wireless-security.pmf required
```

Adresse fixe pour la caméra (remplacer par l'adresse MAC affichée au démarrage
de la caméra sur le moniteur série, ou relevée dans `journalctl -u NetworkManager`) :

```bash
echo 'dhcp-host=AA:BB:CC:DD:EE:FF,10.42.0.20' | \
  sudo tee /etc/NetworkManager/dnsmasq-shared.d/camera.conf
sudo nmcli con down arrivee && sudo nmcli con up arrivee
```

La tablette des bénévoles se connecte au même réseau et ouvre `http://10.42.0.1:8000`.

## 2. Heure (NTP)

Les temps sont calculés par différence avec l'heure de départ, donnée par le Pi :
seule compte la **cohérence** entre le Pi et la caméra, synchronisée chaque minute.

```bash
sudo apt install -y chrony
printf 'allow 10.42.0.0/24\nlocal stratum 10\n' | sudo tee /etc/chrony/conf.d/arrivee.conf
sudo systemctl restart chrony
```

Sans Internet sur place, l'heure du Pi 5 vient de son horloge RTC : y brancher la
pile RTC officielle, ou connecter le Pi à Internet une fois avant la course, pour
que l'heure affichée soit juste.

## 3. Service du chrono

```bash
sudo apt install -y python3-venv git
git clone https://github.com/f4b13n-l4b/esp32s3-cam.git ~/esp32s3-cam
cd ~/esp32s3-cam/pi
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml && nano config.toml   # jeton caméra, code bénévole
mkdir -p data                                           # seul dossier inscriptible par le service
sudo cp ../deploy/finishline.service /etc/systemd/system/
sudo systemctl enable --now finishline
journalctl -u finishline -f
```

Le fichier de service suppose l'utilisateur `pi` et le dépôt dans `/home/pi/esp32s3-cam` :
adapter `User=` et les chemins sinon.

## 4. Après la course

Exporter le CSV, puis supprimer les images (`data/course_*/`) une fois les
résultats validés. La base `data/chrono.sqlite3` ne contient que dossards et temps.
