----CameraSuveillance

Un petit programme pour Windows qui surveille avec le micro et la webcam de ton PC.

Quand il entend un bruit (pas, porte, voix...), il prend une photo et filme quelques secondes. Tu peux aussi l'activer ou le couper depuis ton téléphone.

----Ce qu'il faut avoir

- Un PC Windows 10 ou 11
- Python 3.9 ou plus récent : https://www.python.org/downloads/
  (pendant l'installation, coche bien "Add python.exe to PATH")
- Un micro et une webcam

----Installation

1. Télécharge ce projet (bouton vert "Code", puis "Download ZIP") et décompresse-le.
2. Ouvre l'invite de commandes dans le dossier du projet.
3. Installe les modules avec cette commande :

pip install -r requirements.txt

----Lancer le programme

Dans l'invite de commandes :

py CameraSuveillance.pyw

Tu peux aussi faire un double-clic sur le fichier CameraSuveillance.pyw.

----Comment l'utiliser

1. Ouvre l'onglet *Réglages* et change le mot de passe (celui par défaut est change-moi). Clique sur *Enregistrer*.
2. Clique sur *Tester la webcam* pour vérifier que l'image s'affiche.
3. Clique sur *ACTIVER LA SURVEILLANCE*.

Le programme écoute alors le micro. À chaque bruit détecté, il enregistre une photo et une vidéo.

Si tu as trop de fausses alertes, augmente la valeur de sensibilité. S'il ne détecte rien, baisse-la.

---- Voir et activer depuis ton téléphone

1. Dans le programme, ouvre l'onglet *Web à distance* et note l'adresse affichée.
2. Si ton téléphone est sur le même Wi-Fi, ouvre cette adresse dans le navigateur et entre ton mot de passe.
3. Si tu es loin de chez toi, installe l'application gratuite *Tailscale* (https://tailscale.com/download) sur le PC et sur le téléphone, avec le même compte. Ouvre ensuite l'adresse Tailscale du PC suivie de :5000.

Important : n'ouvre jamais ton PC directement sur Internet depuis ta box. Utilise un mot de passe long et personnel.

----Où sont mes photos et mes vidéos ?

Elles sont rangées dans ton dossier Windows, pas dans le dossier du projet :

%APPDATA%\CameraSuveillance

Copie cette ligne dans la barre d'adresse de l'Explorateur de fichiers pour l'ouvrir. Ton mot de passe y est aussi enregistré. Rien de privé n'est donc publié sur GitHub.

----Pour que ça marche la nuit

- Dans Windows, mets la mise en veille sur "Jamais" quand le PC est branché.
- Sur un portable, règle "Fermer le capot" sur "Ne rien faire".
- Dans les Réglages du programme, tu peux cocher "Lancer avec Windows" pour qu'il démarre tout seul.

Un PC éteint ou mis en veille ne peut rien surveiller.

----Si ça ne marche pas

- Message "No module named..." : refais pip install -r requirements.txt.
- La webcam ne s'ouvre pas : ferme les autres programmes qui l'utilisent, ou change son numéro dans les Réglages.
- Micro ou caméra bloqués : dans Windows, va dans Paramètres, Confidentialité, et autorise le micro et la caméra.

----Créer un fichier .exe (facultatif)

Si tu veux un programme qui se lance sans Python :

pip install pyinstaller pillow

py creer_icone.py

py -m PyInstaller --onefile --noconsole --icon icone_surveillance.ico --name CameraSuveillance --collect-all sounddevice --collect-all _sounddevice_data CameraSuveillance.pyw

Le programme sera dans le dossier dist.

## À lire avant de l'utiliser

Utilise ce programme seulement chez toi, ou dans un endroit où tu as le droit de filmer et d'enregistrer, et préviens les personnes concernées.
