#pragma once
// Copier ce fichier en « secrets.h » (ignoré par git) et renseigner les valeurs.

// Réseau Wi-Fi émis par le Raspberry Pi (point d'accès local de l'arrivée)
#define WIFI_SSID     "arrivee-automnales"
#define WIFI_PASSWORD "changez-moi-mot-de-passe-long"

// Serveur NTP : l'adresse du Pi sur le réseau local
#define NTP_SERVER    "10.42.0.1"

// Jeton exigé par la caméra (en-tête « Authorization: Bearer <jeton> »).
// Générer avec : python3 -c "import secrets; print(secrets.token_urlsafe(24))"
#define CAMERA_TOKEN  "remplacez-par-un-jeton-aleatoire"
