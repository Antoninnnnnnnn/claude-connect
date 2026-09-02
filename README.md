# Claude Connect

APIs FastAPI auto-hebergees pour exposer des services web a des assistants LLM (Claude, etc.) via HTTP JSON compact.

Chaque connecteur tourne en service independant. Une cle API commune protege tous les endpoints.

## Services

| Service | Port | Dossier | Doc LLM |
|---------|------|---------|---------|
| Vinted | 8091 | [`vinted/`](vinted/) | [`LLM_VINTED_USAGE.md`](LLM_VINTED_USAGE.md) |
| Leboncoin | 8092 | [`leboncoin/`](leboncoin/) | [`LLM_LEBONCOIN_USAGE.md`](LLM_LEBONCOIN_USAGE.md) |
| EcoleDirecte | 8093 | [`ecoledirecte/`](ecoledirecte/) | [`LLM_ECOLEDIRECTE_USAGE.md`](LLM_ECOLEDIRECTE_USAGE.md) |
| La Centrale | 8094 | [`lacentrale/`](lacentrale/) | [`LLM_CENTRALE_USAGE.md`](LLM_CENTRALE_USAGE.md) |

## Demarrage rapide

```bash
git clone https://github.com/Antoninnnnnnnn/claude-connect.git
cd claude-connect
cp .env.example .env
# editer .env : definir API_KEY et les variables utiles
```

Lancer un service (exemple Vinted) :

```bash
cd vinted
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
set -a && source ../.env && set +a
uvicorn app.main:app --host 0.0.0.0 --port 8091
```

OpenAPI : `http://127.0.0.1:8091/docs`

## Configuration

Fichier commun a la racine : `.env` (voir `.env.example`).

Chaque service ecoute en loopback (`127.0.0.1`) par defaut : seul le reverse proxy local doit les joindre. Surcharger avec `<SERVICE>_HOST` si besoin.

Authentification sur tous les endpoints proteges :

```http
X-API-Key: <API_KEY>
```

Ne jamais committer `.env`, les cookies de session, ni les reponses QCM EcoleDirecte.

## Exposer a Claude.ai

1. Heberger les APIs sur un VPS (systemd ou process manager).
2. Placer un reverse proxy HTTPS devant chaque service — voir [`deploy/nginx.example.conf`](deploy/nginx.example.conf).
3. Donner a l'assistant le fichier `LLM_*_USAGE.md` correspondant (instructions curl, parametres, regles de contexte).

Les docs `LLM_*_USAGE.md` du repo sont des modeles generiques (`<API_KEY>`, `127.0.0.1`). Pour un deploiement perso avec domaine et cle reelles, copier vers `*.local.md` (fichiers ignores par git).

## Exemples curl

Vinted :

```bash
curl -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:8091/search?query=nike&price_max=30&domain=fr&per_page=5"
```

Leboncoin :

```bash
curl -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:8092/search?text=nike&category=sneakers&price_max=80&sort=newest&limit=5"
```

La Centrale :

```bash
curl -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:8094/search?make=RENAULT&model=ZOE&price_max=8000&zip=27000&distance_km=5&limit=5"
```

## Reponses compactes

Par defaut, chaque API renvoie un JSON minimal pour limiter le contexte LLM. Les champs enrichis (images, vendeur, raw upstream) sont opt-in via des query params (`include_photo`, `include_image`, `raw`, etc.) — voir la doc de chaque service.

## Systemd

Exemples de unites utilisateur dans chaque sous-dossier (`*.service.example`). Adapter les chemins absolus avant installation, puis :

```bash
cp vinted/vinted-api.service.example ~/.config/systemd/user/vinted-api.service
systemctl --user daemon-reload
systemctl --user enable --now vinted-api
```

## La Centrale : navigateur pour DataDome

Le HTML de `www.lacentrale.fr` (description, vendeur) exige une clearance DataDome frappee par un vrai navigateur. Elle vit dans un virtualenv separe :

```bash
python3 -m venv .venv-browser
.venv-browser/bin/pip install -r lacentrale/requirements-browser.txt
.venv-browser/bin/python -m camoufox fetch
```

Le telechargement du navigateur demande environ 1,5 Go d'espace disque libre. Sans lui, la recherche et les fiches JSON fonctionnent, mais les descriptions sont indisponibles.

## Licence

Usage personnel / auto-heberge. Les APIs interrogent des services tiers (Vinted, Leboncoin, La Centrale, EcoleDirecte) : respecter leurs conditions d'utilisation.
