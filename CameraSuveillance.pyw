# -*- coding: utf-8 -*-
"""
SURVEILLANCE SON + WEBCAM  -  programme tout-en-un avec interface graphique

Le micro écoute. Au moindre bruit inhabituel, la webcam prend une photo et filme.
Tout se règle ici : sensibilité, durée, mot de passe, serveur web à distance,
démarrage automatique avec Windows, galerie des captures.

Installation (une seule fois, dans l'invite de commandes) :
    pip install sounddevice numpy opencv-python flask

Lancement : double-clic sur ce fichier.
"""
import base64
import ctypes
import hmac
import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

# Mode sans console (pythonw) : évite les plantages quand stdout n'existe pas
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

MANQUANTS = []
try:
    import numpy as np
except ImportError:
    MANQUANTS.append("numpy")
try:
    import sounddevice as sd
except Exception:
    MANQUANTS.append("sounddevice")
try:
    import cv2
except ImportError:
    MANQUANTS.append("opencv-python")
try:
    from flask import (Flask, jsonify, redirect, render_template_string, request,
                       send_from_directory, session)
    from werkzeug.serving import make_server
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    FLASK_OK = True
except ImportError:
    FLASK_OK = False

# ----------------------------------------------------------------- chemins
DOSSIER_APP = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(DOSSIER_APP, "config_surveillance.json")
DOSSIER = os.path.join(DOSSIER_APP, "captures")
if not DOSSIER.isascii():   # OpenCV écrit mal dans les chemins avec accents
    DOSSIER = os.path.join(os.environ.get("PUBLIC", "C:\\Users\\Public"), "SurveillanceCaptures")

DEFAUT = {
    "sensibilite": 1.5,        # 1.1 = extrême ... 4 = faible
    "duree_video": 15,
    "pause": 5,
    "camera": 0,
    "mot_de_passe": "change-moi",
    "port": 5000,
    "web_actif": True,
    "auto_activer": False,
    "demarrage_windows": False,
}


def charger_cfg():
    cfg = dict(DEFAUT)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def sauver_cfg(cfg):
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def ouvrir(chemin):
    try:
        if os.name == "nt":
            os.startfile(chemin)
        else:
            subprocess.Popen(["xdg-open", chemin])
    except Exception:
        pass


def ip_locale():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def vers_photo(img, largeur):
    """Image OpenCV -> PhotoImage Tk (sans Pillow)."""
    h, w = img.shape[:2]
    if w != largeur:
        img = cv2.resize(img, (largeur, int(h * largeur / w)))
    ok, buf = cv2.imencode(".png", img)
    return tk.PhotoImage(data=base64.b64encode(buf.tobytes()))


# ----------------------------------------------------------------- moteur
FS = 16000
BLOC = 0.1
FREQ_MIN, FREQ_MAX = 80, 5000


def energie(bloc):
    spectre = np.abs(np.fft.rfft(bloc))
    freqs = np.fft.rfftfreq(len(bloc), 1 / FS)
    masque = (freqs >= FREQ_MIN) & (freqs <= FREQ_MAX)
    return float(np.sqrt(np.mean(spectre[masque] ** 2))) / len(bloc)


def garder_eveille(oui):
    """Empêche la mise en veille de Windows (l'écran peut s'éteindre)."""
    if os.name != "nt":
        return
    flags = 0x80000000 | (0x00000001 if oui else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


class Moteur:
    def __init__(self, cfg, journal):
        self.cfg = cfg
        self.log = journal
        self.actif = False
        self.etat = "Arrêtée"
        self.nb = 0
        self.derniere = None
        self.niveau = 0.0
        self.seuil = 1.0
        self._stop = threading.Event()
        self._thread = None
        self._cam_verrou = threading.Lock()

    # ---- webcam
    def _ouvrir_cam(self):
        idx = int(self.cfg["camera"])
        if os.name == "nt":
            return cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        return cv2.VideoCapture(idx)

    def photo_test(self):
        with self._cam_verrou:
            cam = self._ouvrir_cam()
            if not cam.isOpened():
                return None
            for _ in range(8):
                cam.read()
            ok, img = cam.read()
            cam.release()
            return img if ok else None

    def filmer(self, interruptible=True):
        with self._cam_verrou:
            os.makedirs(DOSSIER, exist_ok=True)
            cam = self._ouvrir_cam()
            if not cam.isOpened():
                self.log("Impossible d'ouvrir la webcam (déjà utilisée ? mauvais numéro ?).")
                return None
            for _ in range(8):
                cam.read()
            ok, image = cam.read()
            if not ok:
                cam.release()
                self.log("La webcam n'a pas renvoyé d'image.")
                return None
            nom = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            cv2.imwrite(os.path.join(DOSSIER, nom + ".jpg"), image)
            h, w = image.shape[:2]
            video = cv2.VideoWriter(os.path.join(DOSSIER, nom + ".mp4"),
                                    cv2.VideoWriter_fourcc(*"mp4v"), 20, (w, h))
            fin = time.time() + float(self.cfg["duree_video"])
            while time.time() < fin and not (interruptible and self._stop.is_set()):
                ok, image = cam.read()
                if ok:
                    cv2.putText(image, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), (10, 25),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
                    video.write(image)
            video.release()
            cam.release()
            self.log(f"Capture enregistrée : {nom}")
            return nom

    # ---- surveillance
    def demarrer(self):
        if self.actif:
            return False
        self._stop.clear()
        self.actif = True
        self.etat = "Calibration..."
        self._thread = threading.Thread(target=self._boucle, daemon=True)
        self._thread.start()
        self.log("Surveillance activée.")
        return True

    def arreter(self):
        if not self.actif:
            return False
        self.etat = "Arrêt en cours..."
        self._stop.set()
        return True

    def _boucle(self):
        garder_eveille(True)
        try:
            while not self._stop.is_set():
                try:
                    self._ecouter()
                except Exception as erreur:
                    self.log(f"Erreur : {erreur} - nouvelle tentative dans 5 s")
                    self.etat = "Erreur, nouvelle tentative..."
                    self._stop.wait(5)
        finally:
            garder_eveille(False)
            self.actif = False
            self.etat = "Arrêtée"
            self.niveau = 0.0
            self.log("Surveillance arrêtée.")

    def _ecouter(self):
        q = queue.Queue()

        def callback(indata, frames, temps, statut):
            q.put(indata[:, 0].copy())

        with sd.InputStream(samplerate=FS, channels=1, blocksize=int(FS * BLOC),
                            callback=callback):
            self.etat = "Calibration..."
            valeurs = []
            fin = time.time() + 3
            while time.time() < fin and not self._stop.is_set():
                try:
                    valeurs.append(energie(q.get(timeout=0.5)))
                except queue.Empty:
                    pass
            base = max(float(np.mean(valeurs)) if valeurs else 1e-6, 1e-6)

            self.etat = "Surveillance active"
            precedent, consecutifs = base, 0
            dernier = time.time()
            while not self._stop.is_set():
                try:
                    e = energie(q.get(timeout=0.5))
                    dernier = time.time()
                except queue.Empty:
                    if time.time() - dernier > 5:
                        raise RuntimeError("le micro ne répond plus")
                    continue
                self.niveau = e
                self.seuil = base * float(self.cfg["sensibilite"])
                soudain = e > precedent * 2.5 and e > base * 1.2
                precedent = e
                if e > self.seuil or soudain:
                    consecutifs += 1
                else:
                    consecutifs = 0
                    base = 0.98 * base + 0.02 * e
                if consecutifs >= 1:
                    self.etat = "Détection ! Enregistrement..."
                    self.log("Bruit détecté -> webcam")
                    if self.filmer():
                        self.nb += 1
                        self.derniere = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                    self.etat = "Pause..."
                    self._stop.wait(float(self.cfg["pause"]))
                    with q.mutex:
                        q.queue.clear()
                    consecutifs = 0
                    dernier = time.time()
                    self.etat = "Surveillance active"


# ----------------------------------------------------------------- serveur web
PAGE_LOGIN = """
<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connexion</title>
<style>body{font-family:system-ui;background:#0f172a;color:#e2e8f0;display:grid;place-items:center;height:100vh;margin:0}
form{background:#1e293b;padding:24px;border-radius:14px;width:min(320px,88vw)}
input,button{width:100%;padding:12px;margin-top:10px;border-radius:8px;border:0;font-size:16px;box-sizing:border-box}
button{background:#0ea5e9;color:#fff;font-weight:600}.e{color:#fca5a5}</style></head><body>
<form method="post"><h2>Surveillance</h2>
<input type="password" name="mdp" placeholder="Mot de passe" autofocus>
{% if erreur %}<div class="e">Mot de passe incorrect</div>{% endif %}
<button>Se connecter</button></form></body></html>
"""

PAGE = """
<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Surveillance</title>
<style>
body{font-family:system-ui;background:#0f172a;color:#e2e8f0;margin:0;padding:16px;max-width:760px;margin:auto}
.c{background:#1e293b;border-radius:14px;padding:16px;margin-bottom:14px}
h1{font-size:20px;margin:4px 0 14px}
.etat{font-size:18px;font-weight:700}.on{color:#4ade80}.off{color:#f87171}
button{padding:14px;border:0;border-radius:10px;font-size:16px;font-weight:600;width:100%;margin-top:10px;color:#fff}
#on{background:#16a34a}#off{background:#dc2626}
input[type=range]{width:100%}
.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.g a{color:#e2e8f0;text-decoration:none;font-size:12px}
.g img{width:100%;border-radius:8px;display:block}
small{color:#94a3b8}
</style></head><body>
<h1>Surveillance à distance</h1>
<div class="c">
  <div>État : <span id="etat" class="etat">...</span></div>
  <small id="det"></small>
  <button id="on" onclick="act('activer')">Activer la surveillance</button>
  <button id="off" onclick="act('desactiver')">Désactiver</button>
</div>
<div class="c">
  <b>Sensibilité</b> : <span id="sv"></span><br>
  <input type="range" id="sens" min="1.1" max="4" step="0.1" onchange="regler(this.value)" oninput="sv.textContent=this.value">
  <small>Plus la valeur est basse, plus c'est sensible.</small>
</div>
<div class="c"><b>Captures récentes</b><div class="g" id="liste" style="margin-top:10px"></div></div>
<p><a href="/logout" style="color:#94a3b8">Se déconnecter</a></p>
<script>
const $ = id => document.getElementById(id);
async function maj(){
  const r = await fetch('/api/etat'); if(r.status==401){location='/';return;}
  const d = await r.json();
  $('etat').textContent = d.etat;
  $('etat').className = 'etat ' + (d.actif ? 'on' : 'off');
  $('det').textContent = d.nb + ' détection(s)' + (d.derniere ? ' - dernière : ' + d.derniere : '');
  if(document.activeElement !== $('sens')){ $('sens').value = d.sensibilite; $('sv').textContent = d.sensibilite; }
  $('liste').innerHTML = d.captures.map(c =>
    '<a href="/captures/'+c+'.mp4" target="_blank"><img src="/captures/'+c+'.jpg" loading="lazy">'+c.replace('_',' ')+'</a>').join('');
}
async function act(a){ await fetch('/api/'+a,{method:'POST'}); setTimeout(maj,800); }
async function regler(v){ await fetch('/api/sensibilite',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({valeur:v})}); }
maj(); setInterval(maj, 3000);
</script></body></html>
"""


class ServeurWeb:
    def __init__(self, moteur, cfg, journal):
        self.m, self.cfg, self.log = moteur, cfg, journal
        self.srv = None
        self.actif = False
        self._echecs = {}

    def _app(self):
        app = Flask(__name__)
        app.secret_key = secrets.token_hex(32)
        m, cfg = self.m, self.cfg

        def connecte():
            return session.get("ok") is True

        @app.route("/", methods=["GET", "POST"])
        def accueil():
            if request.method == "POST":
                ip = request.remote_addr
                if self._echecs.get(ip, 0) >= 5:
                    time.sleep(3)
                if hmac.compare_digest(request.form.get("mdp", ""), str(cfg["mot_de_passe"])):
                    session["ok"] = True
                    self._echecs[ip] = 0
                    self.log(f"Connexion web réussie ({ip})")
                    return redirect("/")
                self._echecs[ip] = self._echecs.get(ip, 0) + 1
                self.log(f"Mot de passe web incorrect ({ip})")
                time.sleep(1)
                return render_template_string(PAGE_LOGIN, erreur=True)
            if not connecte():
                return render_template_string(PAGE_LOGIN, erreur=False)
            return render_template_string(PAGE)

        @app.route("/logout")
        def logout():
            session.clear()
            return redirect("/")

        @app.route("/api/etat")
        def etat():
            if not connecte():
                return jsonify(erreur="non connecté"), 401
            os.makedirs(DOSSIER, exist_ok=True)
            noms = sorted((f[:-4] for f in os.listdir(DOSSIER) if f.endswith(".jpg")),
                          reverse=True)[:24]
            return jsonify(actif=m.actif, etat=m.etat, sensibilite=round(float(cfg["sensibilite"]), 1),
                           nb=m.nb, derniere=m.derniere, captures=noms)

        @app.route("/api/activer", methods=["POST"])
        def activer():
            if not connecte():
                return jsonify(erreur="non connecté"), 401
            self.log("Activation demandée depuis le web")
            return jsonify(ok=m.demarrer())

        @app.route("/api/desactiver", methods=["POST"])
        def desactiver():
            if not connecte():
                return jsonify(erreur="non connecté"), 401
            self.log("Désactivation demandée depuis le web")
            return jsonify(ok=m.arreter())

        @app.route("/api/sensibilite", methods=["POST"])
        def sensibilite():
            if not connecte():
                return jsonify(erreur="non connecté"), 401
            try:
                v = float((request.get_json(silent=True) or {}).get("valeur"))
            except (TypeError, ValueError):
                return jsonify(erreur="valeur invalide"), 400
            cfg["sensibilite"] = round(min(max(v, 1.1), 4.0), 1)
            return jsonify(ok=True)

        @app.route("/captures/<path:fichier>")
        def captures(fichier):
            if not connecte():
                return "Non autorisé", 401
            return send_from_directory(DOSSIER, fichier)

        return app

    def demarrer(self):
        if self.actif:
            return True
        try:
            self.srv = make_server("0.0.0.0", int(self.cfg["port"]), self._app(), threaded=True)
        except OSError as e:
            self.log(f"Serveur web impossible (port occupé ?) : {e}")
            return False
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.actif = True
        self.log(f"Serveur web démarré : http://{ip_locale()}:{self.cfg['port']}")
        return True

    def arreter(self):
        if self.srv:
            threading.Thread(target=self.srv.shutdown, daemon=True).start()
        self.actif = False
        self.srv = None
        self.log("Serveur web arrêté.")


# ----------------------------------------------------------------- interface
BG, CARD, TXT, GRIS = "#0f172a", "#1e293b", "#e2e8f0", "#94a3b8"
ACC, VERT, ROUGE, ORANGE = "#0ea5e9", "#16a34a", "#dc2626", "#f59e0b"


def chemin_demarrage():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                        "Start Menu", "Programs", "Startup", "surveillance_app.vbs")


def regler_demarrage_windows(oui):
    if os.name != "nt":
        return False
    cible = chemin_demarrage()
    if not oui:
        try:
            os.remove(cible)
        except OSError:
            pass
        return True
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    commande = f'"{pyw}" "{os.path.abspath(__file__)}" --auto'
    vbs = ('Set sh = CreateObject("WScript.Shell")\r\n'
           f'sh.CurrentDirectory = "{DOSSIER_APP}"\r\n'
           f'sh.Run "{commande.replace(chr(34), chr(34) * 2)}", 7, False\r\n')
    with open(cible, "w", encoding="utf-8") as f:
        f.write(vbs)
    return True


class App(tk.Tk):
    def __init__(self, auto):
        super().__init__()
        self.title("Surveillance Son + Webcam")
        self.geometry("800x900")
        self.minsize(720, 780)
        self.configure(bg=BG)
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        self.cfg = charger_cfg()
        self.q_log, self.q_ui = queue.Queue(), queue.Queue()
        self.moteur = Moteur(self.cfg, self.ecrire_journal)
        self.web = ServeurWeb(self.moteur, self.cfg, self.ecrire_journal) if FLASK_OK else None
        self._miniatures = []
        self._nb_vu = 0

        self._styles()
        self._construire()
        self.protocol("WM_DELETE_WINDOW", self.fermer)

        self.ecrire_journal(f"Dossier des captures : {DOSSIER}")
        if self.web and self.cfg["web_actif"]:
            self.web.demarrer()
        if auto or self.cfg["auto_activer"]:
            self.moteur.demarrer()
        if auto:
            self.iconify()
        self.charger_captures()
        self.rafraichir()

    # ---- utilitaires
    def ecrire_journal(self, msg):
        self.q_log.put(f"[{datetime.now():%d/%m %H:%M:%S}] {msg}\n")

    def _styles(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=CARD, foreground=TXT, padding=(16, 8),
                     font=("Segoe UI", 10, "bold"), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", ACC)], foreground=[("selected", "white")])
        st.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TXT,
                     rowheight=78, borderwidth=0, font=("Segoe UI", 10))
        st.map("Treeview", background=[("selected", ACC)])
        st.configure("Horizontal.TScale", background=BG, troughcolor=CARD)
        st.configure("Vertical.TScrollbar", background=CARD, troughcolor=BG)

    def _carte(self, parent, **kw):
        return tk.Frame(parent, bg=CARD, **kw)

    def _label(self, parent, texte, taille=10, gras=False, couleur=TXT, **kw):
        return tk.Label(parent, text=texte, bg=parent["bg"], fg=couleur,
                        font=("Segoe UI", taille, "bold" if gras else "normal"), **kw)

    def _bouton(self, parent, texte, commande, couleur=ACC, **kw):
        return tk.Button(parent, text=texte, command=commande, bg=couleur, fg="white",
                         activebackground=couleur, activeforeground="white", relief="flat",
                         font=("Segoe UI", 10, "bold"), cursor="hand2", padx=12, pady=6, **kw)

    # ---- construction
    def _construire(self):
        # En-tête : état + gros bouton
        haut = self._carte(self)
        haut.pack(fill="x", padx=14, pady=(14, 8))
        ligne = tk.Frame(haut, bg=CARD)
        ligne.pack(fill="x", padx=16, pady=(14, 4))
        self.dot = tk.Canvas(ligne, width=26, height=26, bg=CARD, highlightthickness=0)
        self.dot.pack(side="left")
        self.pastille = self.dot.create_oval(3, 3, 23, 23, fill=ROUGE, outline="")
        self.lbl_titre = self._label(ligne, "SURVEILLANCE ARRÊTÉE", 16, True)
        self.lbl_titre.configure(bg=CARD)
        self.lbl_titre.pack(side="left", padx=10)
        self.lbl_etat = self._label(haut, "", 10, couleur=GRIS)
        self.lbl_etat.configure(bg=CARD)
        self.lbl_etat.pack(anchor="w", padx=16)
        self.lbl_det = self._label(haut, "", 10, couleur=GRIS)
        self.lbl_det.configure(bg=CARD)
        self.lbl_det.pack(anchor="w", padx=16)
        self.btn_main = tk.Button(haut, text="ACTIVER LA SURVEILLANCE", command=self.basculer,
                                  bg=VERT, fg="white", activeforeground="white", relief="flat",
                                  font=("Segoe UI", 14, "bold"), cursor="hand2", pady=12)
        self.btn_main.pack(fill="x", padx=16, pady=(10, 6))

        # Jauge sonore
        tk.Label(haut, text="Niveau sonore (la ligne blanche = seuil de détection)", bg=CARD,
                 fg=GRIS, font=("Segoe UI", 9)).pack(anchor="w", padx=16)
        self.jauge = tk.Canvas(haut, height=22, bg="#0b1220", highlightthickness=0)
        self.jauge.pack(fill="x", padx=16, pady=(2, 10))

        # Sensibilité
        tk.Label(haut, text="Sensibilité", bg=CARD, fg=TXT, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=16)
        sl = tk.Frame(haut, bg=CARD)
        sl.pack(fill="x", padx=16)
        tk.Label(sl, text="Très sensible", bg=CARD, fg=GRIS, font=("Segoe UI", 9)).pack(side="left")
        tk.Label(sl, text="Peu sensible", bg=CARD, fg=GRIS, font=("Segoe UI", 9)).pack(side="right")
        self.v_sens = tk.DoubleVar(value=float(self.cfg["sensibilite"]))
        self.lbl_sens = tk.Label(haut, text="", bg=CARD, fg=ACC, font=("Segoe UI", 10, "bold"))
        self.lbl_sens.pack(anchor="w", padx=16)
        sc = ttk.Scale(haut, from_=1.1, to=4.0, variable=self.v_sens, command=self._sens_change)
        sc.pack(fill="x", padx=16, pady=(0, 6))
        sc.bind("<ButtonRelease-1>", lambda e: sauver_cfg(self.cfg))
        self._sens_change()

        boutons = tk.Frame(haut, bg=CARD)
        boutons.pack(fill="x", padx=16, pady=(4, 14))
        self._bouton(boutons, "Tester la webcam", self.tester_webcam).pack(side="left")
        self._bouton(boutons, "Enregistrer maintenant", self.enregistrer_maintenant,
                     ORANGE).pack(side="left", padx=8)

        # Onglets
        self.onglets = ttk.Notebook(self)
        self.onglets.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self._onglet_captures()
        self._onglet_web()
        self._onglet_reglages()
        self._onglet_journal()

    def _onglet_captures(self):
        f = tk.Frame(self.onglets, bg=BG)
        self.onglets.add(f, text="Captures")
        barre = tk.Frame(f, bg=BG)
        barre.pack(fill="x", pady=8)
        self._bouton(barre, "Actualiser", self.charger_captures).pack(side="left")
        self._bouton(barre, "Ouvrir le dossier", lambda: (os.makedirs(DOSSIER, exist_ok=True), ouvrir(DOSSIER))
                     ).pack(side="left", padx=8)
        self._bouton(barre, "Supprimer", self.supprimer_captures, ROUGE).pack(side="left")
        tk.Label(f, text="Double-clic sur une ligne : ouvre la vidéo", bg=BG, fg=GRIS,
                 font=("Segoe UI", 9)).pack(anchor="w")
        cadre = tk.Frame(f, bg=BG)
        cadre.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(cadre, columns=("info",), selectmode="extended")
        self.tree.heading("#0", text="Aperçu")
        self.tree.heading("info", text="Date et heure")
        self.tree.column("#0", width=130, stretch=False)
        self.tree.column("info", width=400)
        sb = ttk.Scrollbar(cadre, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.ouvrir_capture)

    def _onglet_web(self):
        f = tk.Frame(self.onglets, bg=BG)
        self.onglets.add(f, text="Web à distance")
        c = self._carte(f)
        c.pack(fill="x", pady=10)
        if not FLASK_OK:
            tk.Label(c, text="Flask n'est pas installé.\nTape dans l'invite de commandes :  pip install flask",
                     bg=CARD, fg=ORANGE, font=("Segoe UI", 11), justify="left").pack(padx=16, pady=16)
            return
        self.v_web = tk.BooleanVar(value=bool(self.cfg["web_actif"]))
        tk.Checkbutton(c, text="Activer le serveur web (commande à distance)", variable=self.v_web,
                       command=self.basculer_web, bg=CARD, fg=TXT, selectcolor=BG, activebackground=CARD,
                       activeforeground=TXT, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=16, pady=(14, 6))
        self.lbl_web = tk.Label(c, text="", bg=CARD, fg=ACC, font=("Consolas", 13, "bold"))
        self.lbl_web.pack(anchor="w", padx=16)
        self._bouton(c, "Copier l'adresse", self.copier_adresse).pack(anchor="w", padx=16, pady=10)
        aide = ("• Sur ton téléphone, connecté au même Wi-Fi : ouvre cette adresse et tape ton mot de passe.\n"
                "• Pour y accéder hors de chez toi : installe Tailscale (gratuit) sur le PC et le téléphone,\n"
                "   puis utilise l'adresse Tailscale du PC avec le même port.\n"
                "• N'ouvre jamais le port sur ta box sans HTTPS. Le mot de passe se change dans Réglages.")
        tk.Label(c, text=aide, bg=CARD, fg=GRIS, font=("Segoe UI", 9), justify="left").pack(anchor="w", padx=16, pady=(0, 14))

    def _onglet_reglages(self):
        f = tk.Frame(self.onglets, bg=BG)
        self.onglets.add(f, text="Réglages")
        c = self._carte(f)
        c.pack(fill="x", pady=10)
        grille = tk.Frame(c, bg=CARD)
        grille.pack(fill="x", padx=16, pady=14)

        def champ(ligne, texte, var, de, a, pas=1):
            tk.Label(grille, text=texte, bg=CARD, fg=TXT, font=("Segoe UI", 10)).grid(row=ligne, column=0, sticky="w", pady=5)
            tk.Spinbox(grille, from_=de, to=a, increment=pas, textvariable=var, width=8, bg=BG, fg=TXT,
                       insertbackground=TXT, buttonbackground=CARD, relief="flat",
                       font=("Segoe UI", 10)).grid(row=ligne, column=1, sticky="w", padx=12)

        self.v_duree = tk.IntVar(value=int(self.cfg["duree_video"]))
        self.v_pause = tk.IntVar(value=int(self.cfg["pause"]))
        self.v_cam = tk.IntVar(value=int(self.cfg["camera"]))
        self.v_port = tk.IntVar(value=int(self.cfg["port"]))
        champ(0, "Durée de chaque vidéo (secondes)", self.v_duree, 3, 300)
        champ(1, "Pause entre deux détections (secondes)", self.v_pause, 0, 120)
        champ(2, "Numéro de la webcam (0 = par défaut)", self.v_cam, 0, 5)
        champ(3, "Port du serveur web", self.v_port, 1024, 65535)

        tk.Label(grille, text="Mot de passe web", bg=CARD, fg=TXT, font=("Segoe UI", 10)).grid(row=4, column=0, sticky="w", pady=5)
        self.v_mdp = tk.StringVar(value=str(self.cfg["mot_de_passe"]))
        self.ent_mdp = tk.Entry(grille, textvariable=self.v_mdp, show="*", width=24, bg=BG, fg=TXT,
                                insertbackground=TXT, relief="flat", font=("Segoe UI", 10))
        self.ent_mdp.grid(row=4, column=1, sticky="w", padx=12)
        self.v_voir = tk.BooleanVar(value=False)
        tk.Checkbutton(grille, text="Afficher", variable=self.v_voir, bg=CARD, fg=GRIS, selectcolor=BG,
                       activebackground=CARD, activeforeground=TXT,
                       command=lambda: self.ent_mdp.configure(show="" if self.v_voir.get() else "*")
                       ).grid(row=4, column=2, sticky="w")

        self.v_auto = tk.BooleanVar(value=bool(self.cfg["auto_activer"]))
        self.v_win = tk.BooleanVar(value=bool(self.cfg["demarrage_windows"]))
        for texte, var in (("Activer la surveillance dès l'ouverture du programme", self.v_auto),
                           ("Lancer le programme avec Windows (en arrière-plan)", self.v_win)):
            tk.Checkbutton(c, text=texte, variable=var, bg=CARD, fg=TXT, selectcolor=BG,
                           activebackground=CARD, activeforeground=TXT,
                           font=("Segoe UI", 10)).pack(anchor="w", padx=16, pady=2)
        tk.Label(c, text="Pendant la surveillance, le PC ne se met pas en veille (l'écran peut s'éteindre).\n"
                         "Pense à régler « Fermer le capot : ne rien faire » sur un portable.",
                 bg=CARD, fg=GRIS, font=("Segoe UI", 9), justify="left").pack(anchor="w", padx=16, pady=(8, 4))
        self._bouton(c, "Enregistrer les réglages", self.enregistrer_reglages, VERT).pack(anchor="w", padx=16, pady=(6, 14))

    def _onglet_journal(self):
        f = tk.Frame(self.onglets, bg=BG)
        self.onglets.add(f, text="Journal")
        self.txt_log = ScrolledText(f, bg=CARD, fg=TXT, insertbackground=TXT, relief="flat",
                                    font=("Consolas", 9), state="disabled", wrap="word")
        self.txt_log.pack(fill="both", expand=True, pady=10)

    # ---- actions
    def basculer(self):
        if self.moteur.actif:
            self.moteur.arreter()
        else:
            self.moteur.demarrer()

    def _sens_change(self, *_):
        v = round(float(self.v_sens.get()), 1)
        self.cfg["sensibilite"] = v
        niveau = "extrême" if v < 1.3 else "très élevée" if v < 1.8 else "moyenne" if v < 2.6 else "faible"
        self.lbl_sens.configure(text=f"{v}  ({niveau})")

    def basculer_web(self):
        if self.v_web.get():
            if not self.web.demarrer():
                self.v_web.set(False)
        else:
            self.web.arreter()
        self.cfg["web_actif"] = self.v_web.get()
        sauver_cfg(self.cfg)

    def copier_adresse(self):
        self.clipboard_clear()
        self.clipboard_append(f"http://{ip_locale()}:{self.cfg['port']}")
        self.ecrire_journal("Adresse copiée.")

    def enregistrer_reglages(self):
        try:
            ancien_port = int(self.cfg["port"])
            self.cfg.update(duree_video=int(self.v_duree.get()), pause=int(self.v_pause.get()),
                            camera=int(self.v_cam.get()), port=int(self.v_port.get()),
                            mot_de_passe=self.v_mdp.get(), auto_activer=self.v_auto.get(),
                            demarrage_windows=self.v_win.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("Réglages", "Une des valeurs n'est pas un nombre valide.")
            return
        if not self.cfg["mot_de_passe"]:
            messagebox.showwarning("Réglages", "Le mot de passe ne peut pas être vide.")
            self.cfg["mot_de_passe"] = "change-moi"
            self.v_mdp.set("change-moi")
        if self.cfg["mot_de_passe"] == "change-moi":
            messagebox.showwarning("Mot de passe", "Tu utilises encore le mot de passe par défaut « change-moi ».\n"
                                                   "Choisis-en un long et personnel avant d'utiliser le web à distance.")
        sauver_cfg(self.cfg)
        try:
            regler_demarrage_windows(self.cfg["demarrage_windows"])
        except OSError as e:
            self.ecrire_journal(f"Démarrage Windows impossible : {e}")
        if self.web and self.web.actif and self.cfg["port"] != ancien_port:
            self.web.arreter()
            self.after(800, self.web.demarrer)
        self.ecrire_journal("Réglages enregistrés.")
        messagebox.showinfo("Réglages", "Réglages enregistrés.")

    def tester_webcam(self):
        self.ecrire_journal("Test de la webcam...")

        def tache():
            img = self.moteur.photo_test()
            self.q_ui.put(lambda: self._montrer_photo(img))
        threading.Thread(target=tache, daemon=True).start()

    def _montrer_photo(self, img):
        if img is None:
            messagebox.showerror("Webcam", "Impossible d'ouvrir la webcam.\nVérifie qu'aucun autre programme "
                                           "ne l'utilise, ou change son numéro dans Réglages.")
            return
        top = tk.Toplevel(self, bg=BG)
        top.title("Aperçu de la webcam")
        top.photo = vers_photo(img, 640)
        tk.Label(top, image=top.photo, bg=BG).pack(padx=10, pady=10)

    def enregistrer_maintenant(self):
        self.ecrire_journal("Enregistrement manuel...")

        def tache():
            self.moteur.filmer(interruptible=False)
            self.q_ui.put(self.charger_captures)
        threading.Thread(target=tache, daemon=True).start()

    # ---- captures
    def charger_captures(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._miniatures = []
        if not os.path.isdir(DOSSIER):
            return
        noms = sorted((f[:-4] for f in os.listdir(DOSSIER) if f.endswith(".jpg")), reverse=True)[:40]
        for nom in noms:
            img = cv2.imread(os.path.join(DOSSIER, nom + ".jpg"))
            photo = vers_photo(img, 104) if img is not None else None
            if photo:
                self._miniatures.append(photo)
            try:
                d = datetime.strptime(nom, "%Y-%m-%d_%H-%M-%S").strftime("%d/%m/%Y  %H:%M:%S")
            except ValueError:
                d = nom
            kw = {"image": photo} if photo else {}
            self.tree.insert("", "end", iid=nom, text="", values=(d,), **kw)

    def ouvrir_capture(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        mp4 = os.path.join(DOSSIER, sel[0] + ".mp4")
        ouvrir(mp4 if os.path.exists(mp4) else os.path.join(DOSSIER, sel[0] + ".jpg"))

    def supprimer_captures(self):
        sel = self.tree.selection()
        if not sel:
            return
        if not messagebox.askyesno("Supprimer", f"Supprimer {len(sel)} capture(s) (photo + vidéo) ?"):
            return
        for nom in sel:
            for ext in (".jpg", ".mp4"):
                try:
                    os.remove(os.path.join(DOSSIER, nom + ext))
                except OSError:
                    pass
        self.charger_captures()

    # ---- boucle d'affichage
    def rafraichir(self):
        try:
            while True:
                self.q_ui.get_nowait()()
        except queue.Empty:
            pass
        lignes = []
        try:
            while True:
                lignes.append(self.q_log.get_nowait())
        except queue.Empty:
            pass
        if lignes:
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", "".join(lignes))
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")

        m = self.moteur
        if m.actif:
            self.dot.itemconfig(self.pastille, fill=VERT)
            self.lbl_titre.configure(text="SURVEILLANCE ACTIVE")
            self.btn_main.configure(text="DÉSACTIVER LA SURVEILLANCE", bg=ROUGE, activebackground=ROUGE)
        else:
            self.dot.itemconfig(self.pastille, fill=ROUGE)
            self.lbl_titre.configure(text="SURVEILLANCE ARRÊTÉE")
            self.btn_main.configure(text="ACTIVER LA SURVEILLANCE", bg=VERT, activebackground=VERT)
        self.lbl_etat.configure(text=f"État : {m.etat}")
        self.lbl_det.configure(text=f"{m.nb} détection(s)" + (f"  -  dernière : {m.derniere}" if m.derniere else ""))

        # jauge
        self.jauge.delete("all")
        w = max(self.jauge.winfo_width(), 10)
        h = 22
        if m.actif and m.seuil > 0:
            ratio = min(1.0, m.niveau / (m.seuil * 2))
            self.jauge.create_rectangle(0, 0, w * ratio, h, fill=ROUGE if ratio >= 0.5 else VERT, outline="")
            self.jauge.create_line(w / 2, 0, w / 2, h, fill="white", width=2)

        if m.nb != self._nb_vu:
            self._nb_vu = m.nb
            self.charger_captures()
        if self.web:
            if self.web.actif:
                self.lbl_web.configure(text=f"http://{ip_locale()}:{self.cfg['port']}")
            else:
                self.lbl_web.configure(text="Serveur web arrêté")
        self.after(150, self.rafraichir)

    def fermer(self):
        if self.moteur.actif:
            r = messagebox.askyesnocancel(
                "Quitter",
                "La surveillance est active.\n\nOui = réduire dans la barre des tâches (elle continue)\n"
                "Non = quitter et l'arrêter\nAnnuler = rester")
            if r is None:
                return
            if r:
                self.iconify()
                return
        self.moteur.arreter()
        if self.web:
            self.web.arreter()
        sauver_cfg(self.cfg)
        self.destroy()


def main():
    if MANQUANTS:
        racine = tk.Tk()
        racine.withdraw()
        messagebox.showerror("Modules manquants",
                             "Ouvre l'invite de commandes Windows et tape :\n\n"
                             f"pip install {' '.join(MANQUANTS)} flask\n\nPuis relance le programme.")
        return
    App("--auto" in sys.argv).mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            racine = tk.Tk()
            racine.withdraw()
            messagebox.showerror("Erreur", traceback.format_exc())
        except Exception:
            pass
