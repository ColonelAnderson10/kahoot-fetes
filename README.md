# 🎄 Kahoot des fêtes

Petite application web pour récolter les photos et les citations gênantes de vos amis, puis les exporter pour créer un Kahoot.

- **Amis** : code d'accès partagé → prénom → envoi de photos (autant qu'ils veulent, une description par photo) ou de citations. Pensé pour le cellulaire.
- **Admin** (`/admin`) : galerie filtrable, modification, suppression, sélection « retenu pour le Kahoot », export ZIP (photos `001_prenom.jpg`…) + CSV.
- Les photos sont converties en JPEG (1920 px max), réorientées et **débarrassées de toutes leurs métadonnées** (dont la géolocalisation).

Stack : Python, FastAPI, Jinja2, SQLite, Pillow + pillow-heif. Voir [SECURITE.md](SECURITE.md) pour l'audit de sécurité.

```
app/            code de l'application (routes, sécurité, images, export, gabarits, CSS/JS)
tests/          tests fonctionnels + tests d'attaque (pytest)
scripts/        sauvegarde.sh, funnel.sh (Unraid)
tailscale/      configurations Tailscale Funnel (public / privé)
data/           base SQLite + photos (créé automatiquement, jamais versionné)
```

---

## 1. Lancement en local (Windows, PowerShell)

```powershell
cd "chemin\vers\kahoot-fetes"

# Environnement virtuel (une seule fois)
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt

# Configuration (une seule fois) : copiez puis modifiez le fichier
Copy-Item .env.example .env
```

Dans `.env`, choisissez un vrai code et un vrai mot de passe, et pour le local (HTTP sans HTTPS) :

```
ENVIRONNEMENT=developpement
COOKIES_SECURISES=false
```

Puis lancez :

```powershell
uvicorn app.main:app_factory --factory --reload --port 8000
```

- Amis : http://localhost:8000
- Admin : http://localhost:8000/admin
- Tester depuis votre cellulaire sur le même Wi-Fi : ajoutez `--host 0.0.0.0` et ouvrez `http://<IP-de-votre-PC>:8000`.

> Si `Activate.ps1` est bloqué : `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

### Tests et analyses de sécurité

```powershell
python -m pytest                          # 77 tests (fonctionnels + attaques)
python -m bandit -r app                   # analyse statique
python -m pip_audit -r requirements.txt   # vulnérabilités connues des dépendances
```

---

## 2. Déploiement sur Unraid 7

Architecture : deux conteneurs dans un même projet Compose.

- `kahoot-fetes` : l'application (non-root, système de fichiers en lecture seule sauf `data/`).
- `kahoot-fetes-tailscale` : un nœud Tailscale **dédié à cette app**. Funnel publie seulement ce conteneur en HTTPS (`https://kahoot-fetes.<votre-tailnet>.ts.net`) ; rien d'autre de votre serveur n'est exposé.
- Le port **8090** donne aussi accès à l'app sur votre réseau local.

### 2.1 Préparer Tailscale (une seule fois, dans https://login.tailscale.com/admin)

1. **DNS** → activez *MagicDNS* et *HTTPS Certificates*.
2. **Access controls** → autorisez Funnel. Ajoutez un tag et l'attribut `funnel` dans la politique :
   ```json
   "tagOwners": { "tag:kahoot": ["autogroup:admin"] },
   "nodeAttrs": [ { "target": ["tag:kahoot"], "attr": ["funnel"] } ]
   ```
3. **Settings → Keys** → *Generate auth key* : non réutilisable, avec le tag `tag:kahoot`. Copiez la clé `tskey-auth-…`.

### 2.2 Installer l'application

Dans le terminal Unraid (icône `>_` en haut à droite) :

```bash
# Clonage du dépôt (le dossier doit être vide ou inexistant)
git clone <URL-de-votre-depot> /mnt/user/appdata/kahoot-fetes
cd /mnt/user/appdata/kahoot-fetes

# Dossier des données, appartenant à l'utilisateur du conteneur (nobody:users)
mkdir -p data
chown -R 99:100 data

# Configuration : copiez puis éditez (code, mot de passe admin, TS_AUTHKEY)
cp .env.example .env
nano .env
chmod 600 .env
```

Dans `.env` sur le serveur, gardez `ENVIRONNEMENT=production` et **ne mettez pas** `COOKIES_SECURISES=false`.

> Pour générer de bons secrets : `openssl rand -base64 18` (mot de passe admin). Pour le code des amis, une phrase facile à dicter suffit, par exemple `sapin-tuque-49-gravy`.

### 2.3 Lancer avec Compose Manager

Préparez d'abord la configuration Tailscale en mode **privé** (pas encore public) :

```bash
cd /mnt/user/appdata/kahoot-fetes
bash scripts/funnel.sh off
docker compose up -d --build
```

> Vous pouvez aussi gérer le projet depuis l'interface **Docker → Compose** du plugin Compose Manager : *Add New Stack*, nom `kahoot-fetes`, et dans les options avancées, indiquez le dossier `/mnt/user/appdata/kahoot-fetes` comme emplacement de la pile. Les boutons *Compose Up / Down* font alors la même chose que les commandes ci-dessus.

Vérifications :

```bash
docker compose ps                                   # les deux conteneurs « running », app « healthy »
curl -s http://localhost:8090/health                # {"statut":"ok"}
docker logs kahoot-fetes-tailscale | tail -20       # le nœud est connecté
```

Testez ensuite sur http://IP-du-serveur:8090 depuis votre réseau local.

### 2.4 Activer / désactiver l'accès public (Funnel, ce conteneur seulement)

```bash
cd /mnt/user/appdata/kahoot-fetes
bash scripts/funnel.sh on       # public : https://kahoot-fetes.<tailnet>.ts.net
bash scripts/funnel.sh status   # état actuel
bash scripts/funnel.sh off      # retour au privé (tailnet seulement)
```

Aucun redémarrage n'est nécessaire : le conteneur Tailscale relit `tailscale/config/serve.json` automatiquement. Pour tout couper : `docker compose stop`.

**À vérifier une fois public** : ouvrez le site depuis votre cellulaire **en 4G/5G** (hors Wi-Fi), puis regardez `docker logs kahoot-fetes | tail`. Les lignes doivent montrer l'IP de votre cellulaire et **non** `127.0.0.1`. Sinon, la limitation par IP s'appliquerait à tout le monde à la fois (voir SECURITE.md, R1).

### 2.5 Mettre à jour

```bash
cd /mnt/user/appdata/kahoot-fetes
git pull
docker compose up -d --build
```

### 2.6 Persistance et sauvegardes

Toutes les données sont dans `/mnt/user/appdata/kahoot-fetes/data` (monté sur `/app/data`). Pour vérifier qu'elles survivent à un redémarrage :

```bash
ls data/uploads | wc -l
docker compose restart app
ls data/uploads | wc -l          # même nombre ; les photos restent visibles dans /admin
```

Sauvegarde (base cohérente + photos, 10 archives conservées) :

```bash
bash scripts/sauvegarde.sh                       # vers /mnt/user/backups/kahoot-fetes
bash scripts/sauvegarde.sh /mnt/user/autre/dossier
```

Pour l'automatiser : plugin **User Scripts** → nouveau script contenant `bash /mnt/user/appdata/kahoot-fetes/scripts/sauvegarde.sh`, planifié chaque jour.

### 2.7 Scanner l'image Docker (Trivy)

```bash
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:latest \
  image --severity HIGH,CRITICAL kahoot-fetes:latest
```

### 2.8 Après les fêtes

```bash
bash scripts/funnel.sh off
bash scripts/sauvegarde.sh
docker compose down
```

Révoquez aussi la machine `kahoot-fetes` dans la console Tailscale si vous n'en avez plus besoin.
