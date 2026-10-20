# Matériel et installation sur le terrain

## Liste

| Élément | Remarque |
|---|---|
| Carte ESP32-S3 + caméra (OV2640 / OV3660) | Module N16R8 de préférence (8 Mo de PSRAM) |
| Raspberry Pi 5 + alimentation 27 W | Carte SD ou SSD ; pile RTC conseillée |
| Tablette ou smartphone | Navigateur récent, connecté au Wi-Fi du Pi |
| Potence ou arche à ≈ 4 m | Caméra au-dessus du centre de la ligne, objectif vers le sol |
| Câble USB-C long (5 m) ou alim 5 V locale | Pour la caméra en hauteur |
| Batterie solaire | En onduleur : le secteur alimente, la batterie prend le relais |

## Hauteur et champ de vision

La caméra doit voir les 5 m de large du couloir. Largeur couverte au sol :
`largeur = 2 × hauteur × tan(champ horizontal / 2)`.

| Objectif | Champ horizontal | Hauteur pour 5 m |
|---|---|---|
| Standard (OV2640/OV3660) | ≈ 65° | **≈ 3,9 m** |
| Grand-angle | ≈ 120° | ≈ 1,5 m (image déformée sur les bords) |

Prévoir 20–30 cm de marge de chaque côté. Résultat attendu en 640×480 :
≈ 1,6 cm par pixel, un coureur vu du dessus fait ≈ 28 px de large dans l'image de
travail (320 px) : c'est la valeur `runner_width_px` par défaut.

## Réglage sur place (onglet « Réglage »)

1. Orienter la caméra pour que la **ligne peinte soit horizontale** dans l'image.
2. Ajuster `line_y` pour que la **bande orange recouvre la ligne peinte**.
3. Faire passer un bénévole seul : un cadre vert marqué « 1 ». Puis deux côte à
   côte, épaules collées : « 2 ». Sinon ajuster `runner_width_px` (largeur du cadre
   d'une personne seule, en pixels de travail).
4. Ombres portées comptées comme coureurs : monter `threshold` (25 → 35).
5. Vérifier « Images/s » ≥ 15. En dessous, baisser `quality` côté caméra ou
   rapprocher le Pi.

## Consommation

| Élément | Puissance |
|---|---|
| Raspberry Pi 5 (Wi-Fi AP + détection) | 6 – 9 W |
| ESP32-S3 caméra en flux continu | ≈ 1 W |
| **Total sur 4 h** | **≈ 40 Wh** |
