# SmartFrame — Reste à faire

Les points qui demandent une action sur le cadre lui-même. Supprimer ce fichier quand ils sont faits.

## 1. Calibrer le son

Il faut une appli de mesure du son (en dBA) sur un téléphone, et un moment de silence. Voir aussi [Calibrating the sound level](../README.md#calibrating-the-sound-level).

1. Arrêter le service : `sudo systemctl stop smartframe`.
2. Faire un son régulier autour de 65 dBA près du cadre, puis lancer `.venv/bin/python scripts/calibrate.py`. Le script écrit `calibration_offset_db` (et `calibration_slope` si on mesure un 2e point).
3. Dans une pièce très calme : `.venv/bin/python scripts/measure_noise_floor.py`. Le script écrit `noise_floor_dba`. Sans pièce calme, mettre `33` (valeur de la fiche technique).
4. Optionnel : essayer `audio.mic_compensation: true`, puis refaire les étapes 2 et 3.
5. Relancer le service : `sudo systemctl start smartframe`.

## 2. Capture d'écran du mode audio

Après la calibration, mettre de la musique près du cadre, puis, sur le Pi, en mode `audio` :

```bash
XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0 grim /tmp/smartframe_audio_mode.png
```

La copier dans `images/smartframe_audio_mode.png` (le README l'affiche déjà).

## 3. Déplacer le tag `stable`

Le tag `stable` du Pi pointe sur la dernière version testée. Après chaque mise à jour, une fois la nouvelle version testée sur le Pi (passages `off → audio → mirror → audio → off`, bouton Restart, redémarrage de Home Assistant, journal sans erreur), le déplacer dessus :

```bash
git tag -f stable
```
