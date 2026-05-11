# ORB Bot US100 — Guide de démarrage

## Prérequis
- MT5 installé et connecté à ton broker (US100 visible dans Market Watch)
- Python 3.10+ installé
- Android Studio (pour builder l'app Android)

## 1. Démarrer le backend Python

```cmd
cd "Bot-us100-Backend"

# Créer le venv (première fois uniquement)
py -m venv venv

# Activer le venv
.\venv\Scripts\activate

# Installer les dépendances (première fois uniquement)
pip install -r requirements.txt

# Lancer le backend
python main.py
```

Le backend tourne sur http://localhost:5000

## 2. Lancer l'app Expo

```bash
cd expo
npm install
npm start
```

Un QR code s'affiche dans le terminal.

## 3. Ouvrir sur ton téléphone

1. Installe **Expo Go** sur ton téléphone (App Store ou Play Store)
2. Scanne le QR code avec Expo Go
3. L'app se lance directement — pas besoin de builder quoi que ce soit

## 4. Configurer l'IP dans l'app

- Appuie sur ⚙️ en haut à droite
- Entre l'IP locale de ton PC (visible via `ipconfig` dans le terminal → `IPv4`)
- Ex: `192.168.1.15`
- Ton téléphone et ton PC doivent être sur le **même réseau WiFi**

## Fonctionnement de la stratégie

| Heure (NY) | Ce que fait le bot |
|---|---|
| Avant 9h30 | Attend l'ouverture de session |
| 9h30 → 10h30 | Calcule le range (haut et bas) |
| 10h30 | Place Buy Stop au haut + Sell Stop au bas |
| Trade déclenché | L'autre ordre reste en attente jusqu'à la fin de journée |

## Notes importantes

- Le backend DOIT tourner sur Windows (requis par MT5 Python)
- MT5 doit être ouvert et connecté pendant que le bot tourne
- Teste d'abord en compte DEMO !
