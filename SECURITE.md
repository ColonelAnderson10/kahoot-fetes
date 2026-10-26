# Revue de sécurité — Kahoot des fêtes

Revue réalisée le 25 octobre 2026 avant mise en ligne publique, en se plaçant du point de vue d'un attaquant externe. L'app sera exposée sur Internet via Tailscale Funnel.

## 1. Résumé

| Analyse | Résultat |
|---|---|
| `bandit -r app` (analyse statique, 984 lignes) | **Aucun problème** |
| `pip-audit` (requirements.txt et requirements-dev.txt) | **Aucune vulnérabilité connue** |
| Tests pytest (fonctionnels + 46 tests d'attaque) | **77 / 77 réussis** |
| Trivy (scan de l'image Docker) | **Non exécuté** : Docker n'est pas installé sur le poste de développement. Commande dans le README, section 2.7. |
| Construction et démarrage de l'image Docker | **Non vérifiés ici**, même raison. À vérifier sur Unraid (README 2.3 et 2.6). |

Aucun problème **critique**. Un problème **élevé** et plusieurs problèmes moyens et faibles ont été trouvés pendant la revue, et **tous ont été corrigés**, puis l'analyse et les tests ont été relancés.

## 2. Problèmes trouvés et corrigés

| # | Gravité | Problème | Correction |
|---|---|---|---|
| P1 | **Élevée** | **Épuisement de la mémoire.** Une personne ayant le code pouvait lancer des dizaines d'envois simultanés : chaque photo de 50 Mpx occupe ~150 Mo une fois décodée, ce qui fait tomber le conteneur (limite 1 Go). | Au plus **2 images traitées en même temps** (sémaphore), en plus de la limite de débit. |
| P2 | Moyenne | **Remplissage du disque** par des envois massifs. | Les envois sont refusés quand il reste **moins de 1 Go** libre. |
| P3 | Moyenne | **Changer le code d'accès ne déconnectait personne** : les sessions de 30 jours restaient valides après une fuite du code. | Une empreinte des secrets est mémorisée ; au démarrage, **si un secret a changé, toutes les sessions sont détruites**. |
| P4 | Faible | Un jeton CSRF contenant des accents provoquait une **erreur 500** (`hmac.compare_digest` refuse le texte non ASCII). | Comparaison sur les octets UTF-8 → réponse 403 propre. |
| P5 | Faible | Un **secret refusé au démarrage apparaissait en clair** dans les journaux Docker (message d'erreur de pydantic). | `hide_input_in_errors=True` : seul le nom de la variable est affiché. |
| P6 | Faible | Un export ZIP interrompu (téléchargement annulé) pouvait **laisser une copie des photos retenues** dans `data/tmp`. | Nettoyage de `data/tmp` à chaque démarrage. |
| P7 | Faible | Les compteurs anti-force brute **n'étaient pas bornés en mémoire**, et le limiteur de débit **vidait tous les compteurs** au-delà de 50 000 IP (un attaquant disposant de beaucoup d'IPv6 aurait pu remettre les limites à zéro). | Seules les entrées expirées sont purgées. |
| P8 | Faible | `/tmp` du conteneur (128 Mo) trop petit pour plusieurs envois simultanés de 25 Mo. | Porté à 256 Mo. |

## 3. Vérification point par point (OWASP Top 10)

Chaque ligne « ✅ testé » correspond à un test dans `tests/test_attaques.py`.

### Téléversements
| Contrôle | État |
|---|---|
| Fichier déguisé (script `.jpg`, PHP, HTML, exécutable, JPEG tronqué, fichier vide) | ✅ testé — le **vrai type** est lu dans les premiers octets, puis l'image est entièrement décodée par Pillow ; tout échec = refus. |
| Fichier polyglotte (JPEG valide + script collé à la fin) | ✅ testé — l'image est **ré-encodée** dans une image vierge : rien d'autre que les pixels n'est conservé. |
| Bombe de décompression | ✅ testé — `Image.MAX_IMAGE_PIXELS = 50 Mpx`, et les dimensions sont vérifiées **avant** le décodage (PNG de 144 Mpx refusé). |
| Traversée de répertoire par le nom de fichier | ✅ testé — le nom d'origine n'est **jamais** utilisé : fichier nommé `uuid4.jpg`. |
| Taille par fichier et par requête | ✅ testé — 25 Mo par photo (configurable), corps de requête limité à 26 Mo (Content-Length **et** flux réel), 20 fichiers max par requête. |
| Métadonnées | ✅ testé — EXIF, GPS, marque/modèle, XMP, ICC retirés (JPEG et HEIC). |

### Injections
| Contrôle | État |
|---|---|
| XSS (prénom, description, citation, contexte, filtres admin, page d'erreur) | ✅ testé — échappement automatique Jinja2 partout ; le JavaScript n'utilise que `textContent` ; CSP `script-src 'self'` sans script en ligne. |
| Injection SQL | ✅ testé — **requêtes paramétrées uniquement** (module `sqlite3`). |
| Injection CSV (`=`, `+`, `-`, `@`, tabulation, retour chariot) | ✅ testé — la cellule est préfixée d'une apostrophe dans l'export. |

### Authentification et sessions
| Contrôle | État |
|---|---|
| Force brute sur le code et le mot de passe admin | ✅ testé — **5 échecs par IP → blocage 15 min** ; au-delà de **30 échecs (code) / 20 échecs (admin) au total** toutes IP confondues → blocage global 15 min. |
| Comparaison en temps constant | ✅ — `hmac.compare_digest` sur des empreintes SHA-256 de même longueur. |
| Expiration des sessions | ✅ testé — amis 30 jours, admin 2 h d'inactivité, session anonyme 1 h ; vérifiée côté serveur. |
| Régénération à la connexion (fixation de session) | ✅ testé — nouveau jeton et nouveau jeton CSRF à chaque connexion ; l'ancien est détruit. |
| Stockage des sessions | ✅ — côté serveur (SQLite) ; le cookie ne contient qu'un jeton aléatoire de 256 bits, stocké **haché**. |
| Cookies | ✅ testé — `__Host-` + `Secure` + `HttpOnly` + `SameSite=Lax` ; `Secure` ne peut pas être désactivé en production. |
| Secrets faibles | ✅ testé — l'app **refuse de démarrer** si un secret est absent, trop court (8 / 12 car.), trivial, répétitif, encore égal à `.env.example`, ou si les deux sont identiques. |

### Contrôle d'accès
| Contrôle | État |
|---|---|
| Pages amis sans code | ✅ testé — redirection vers `/acces`. |
| Admin, actions admin et export sans mot de passe (même avec une session ami valide) | ✅ testé — refusés. |
| Photos en devinant l'URL | ✅ testé — `/media/…` est **réservé à l'admin** (réponse 404 pour tous les autres, même les amis : personne ne voit les photos des autres avant le Kahoot). Nom de fichier validé par expression régulière stricte. |
| Traversée d'URL (`../`, encodages `%2e%2e%2f`, `/data/…`) | ✅ testé — 404. |
| Documentation automatique (`/docs`, `/openapi.json`) | ✅ testé — désactivée. |

### CSRF
| Contrôle | État |
|---|---|
| Tous les formulaires (code, prénom, photos, citation, connexion/déconnexion admin, modifier, retenir, supprimer, export) | ✅ testé — jeton lié à la session, vérifié en temps constant ; jeton d'une autre session refusé. |

### En-têtes, erreurs, infrastructure
| Contrôle | État |
|---|---|
| En-têtes de sécurité | ✅ testé — CSP stricte, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, `Cross-Origin-*-Policy`, `Cache-Control: no-store`, **HSTS** en production. En-tête `Server` retiré. |
| Traces d'erreur | ✅ testé — pages d'erreur génériques en français ; aucune trace, aucun chemin, aucun détail de validation. Mode debug désactivé. |
| Limitation de débit | ✅ testé — 120 POST/min (300/min pour les envois de photos, faits en parallèle) et 600 GET/min par IP → 429. Le navigateur patiente et réessaie automatiquement. |
| Proxy (X-Forwarded-*) | ✅ — uvicorn ne fait confiance à `X-Forwarded-For` **que depuis 127.0.0.1**, c'est-à-dire le proxy Tailscale qui partage le réseau du conteneur. Un client du réseau local (port 8090) ne peut pas falsifier son IP. |
| Secrets dans le code, Git ou l'image | ✅ — `.env` et `data/` dans `.gitignore` et `.dockerignore` ; aucun secret dans le code (bandit). Le dépôt Git n'existait pas encore lors de la revue : historique vierge. |
| Docker | ✅ — utilisateur non-root (99:100), `read_only: true` sauf `data/` et `/tmp`, `cap_drop: ALL`, `no-new-privileges`, limites de mémoire et de processus, pas de port privilégié. |

## 4. Risques résiduels (à connaître)

| # | Gravité | Risque | Recommandation |
|---|---|---|---|
| R1 | Moyenne | **L'IP réelle des visiteurs dépend de Tailscale.** Si Funnel ne transmet pas `X-Forwarded-For`, toutes les requêtes publiques sembleront venir de `127.0.0.1` : la limite de débit et le blocage par IP s'appliqueraient alors **à tout le monde à la fois** (5 mauvais codes bloqueraient tous les amis 15 min). | Faire la vérification en 4G du README, section 2.4, dès la mise en ligne. |
| R2 | Moyenne | **Blocage global volontaire** : un attaquant disposant de plusieurs IP peut, avec 30 mauvais codes, bloquer la saisie du code pour tout le monde pendant 15 min (déni de service). Les amis déjà connectés ne sont pas touchés. | Accepté (protège le code contre la force brute distribuée). Garder Funnel désactivé quand la collecte est terminée. |
| R3 | Moyenne | **Toute personne qui a le code peut envoyer n'importe quoi**, sous n'importe quel prénom (pas de comptes individuels), y compris des images choquantes. | Ne partager le code qu'en privé ; le changer dans `.env` en cas de fuite (redémarrer : toutes les sessions sont détruites) ; supprimer depuis `/admin`. |
| R4 | Faible | Les limites de débit et les blocages sont **en mémoire** : ils repartent à zéro au redémarrage du conteneur. | Accepté pour une app à un seul processus. |
| R5 | Faible | **Un seul facteur** pour l'admin (mot de passe). | Mot de passe long et aléatoire. Pour plus de sécurité : n'administrer que depuis le réseau local (port 8090) ou le tailnet. |
| R6 | Faible | La clé `TS_AUTHKEY` est aussi visible dans l'environnement du conteneur de l'app (même fichier `.env`). | Utiliser une clé **non réutilisable** : après la première connexion, elle ne sert plus. |
| R7 | Faible | Secrets en clair dans `.env` sur le serveur ; photos et base **non chiffrées** sur disque et dans les sauvegardes. | `chmod 600 .env` ; sauvegardes en `chmod 600` (fait par le script) ; supprimer les données après les fêtes. |
| R8 | Faible | Les décodeurs d'images (libheif, libwebp, libpng) ont régulièrement des failles. | Mettre à jour Pillow / pillow-heif (`pip-audit`), reconstruire l'image et lancer Trivy avant la mise en ligne. |
| R9 | Faible | Pas de protection contre les connexions lentes (« slowloris ») au-delà de ce que font uvicorn et Tailscale. | Accepté pour un usage privé et temporaire. |
| R10 | Info | Image Docker, Trivy et persistance du volume **non testés sur ce poste** (Docker absent). La persistance a été vérifiée au niveau de l'app (nouvelle instance sur le même dossier `data/`). | Suivre les vérifications du README, sections 2.3, 2.6 et 2.7. |

## 5. Relancer l'audit

```powershell
python -m pytest
python -m bandit -r app
python -m pip_audit -r requirements-dev.txt
```
