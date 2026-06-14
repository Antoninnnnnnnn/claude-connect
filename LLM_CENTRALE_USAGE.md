# Instructions LLM - API La Centrale

Utilise cette API HTTP pour chercher des voitures d'occasion sur La Centrale, lire une annonce, et estimer des prix.

Hub APIs du meme serveur : Leboncoin (`8092`), Vinted (`8091`), voir les docs `LLM_*_USAGE.md`.

## Auth

Ajoute toujours ce header :

```http
X-API-Key: <API_KEY>
```

## Base URL

Locale :

```text
http://127.0.0.1:8094
```

Via reverse proxy HTTPS (optionnel) :

```text
https://<your-domain>/centrale-api
```

## Regles d'utilisation

- Toutes les reponses sont en JSON.
- Une reponse reussie a toujours `{"ok": true, "data": ...}`.
- Une erreur a toujours `{"ok": false, "error": "..."}`.
- Si `ok` vaut `false`, ne devine pas le resultat : rapporte l'erreur.
- Ne pipe pas vers `python3 -m json.tool` ou `jq` sauf debug humain.
- Par defaut, les resultats `/search` sont compacts (pas d'image, pas de dealer, pas de vehicle raw).
- `/listing/{ref}` active `include_image` et `include_vehicle` par defaut.
- Utilise `curl -sS -G --data-urlencode` pour encoder proprement les parametres.
- Ajoute `--connect-timeout 5 --max-time 90` (upstream DataDome + proxy).
- N'utilise `raw=true` que si l'utilisateur demande explicitement le JSON brut.
- Les references annonces sont du type `W102941021` ou `B104008382` (pas des entiers Leboncoin).
- Endpoint detail : `/listing/{ref}` (pas `/ad/`).
- Appelle `/metadata` pour les buckets distance, badges, facettes dynamiques.
- `distance_km` / `distance_bucket` sont des cles UI La Centrale, pas des kilometres litteraux.
- `zip` est requis pour que la distance soit prise en compte cote upstream.
- Les valeurs multiples (`energy`, `regions`, `options`, ...) acceptent des listes separees par des virgules.
- Limite effective par requete : **72** annonces max (3 pages x 24) avec la config actuelle.

## Codes HTTP

| Code | Signification |
|------|---------------|
| 200 | Succes (`ok: true`) |
| 401 | Cle API manquante ou invalide |
| 404 | Annonce introuvable (`/listing/{ref}`) |
| 422 | Parametres invalides (tri, plages min/max, format zip, etc.) |
| 502 | Erreur upstream La Centrale (DataDome, timeout, blocage) |
| 500 | Erreur interne (message generique, pas de stack trace) |

## Pagination

- Taille de page upstream : **24** annonces.
- Le service peut enchainer jusqu'a **3 pages** par requete (`limit` max **72**).
- `page` commence a 1 ; pour aller plus loin, relance avec `page=2`, `page=3`, etc.
- `pagination.total` = total upstream ; `pagination.returned` = nombre renvoye dans cette reponse.

## Buckets distance

`distance_km` / `distance_bucket` sont des indices UI, pas des km reels. `zip` obligatoire.

| UI (`distance_km`) | API (`zipCodeDistance`) |
|--------------------|-------------------------|
| 0 | 0km |
| 5 | 200km |
| 10 | 10km |
| 20 | 20km |
| 30 | 30km |
| 50 | 50km |
| 100 | 100km |
| 200 | 200km |

Exemple : `distance_km=5` avec `zip=27000` → rayon API `200km` (comportement UI La Centrale).

## Curl recommande

```bash
curl -sS --connect-timeout 5 --max-time 90 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8094/search' \
  --data-urlencode 'make=RENAULT' \
  --data-urlencode 'model=ZOE' \
  --data-urlencode 'version=intens' \
  --data-urlencode 'price_max=8000' \
  --data-urlencode 'zip=27000' \
  --data-urlencode 'distance_km=5' \
  --data-urlencode 'energy=ELECTRIC' \
  --data-urlencode 'limit=10'
```

## Endpoint: Search

```http
GET /search
```

Parametres utiles :

```text
make              marque, ex: RENAULT, PEUGEOT
model             modele, ex: ZOE, 208
version           finition, ex: intens, business
price_min         prix minimum EUR
price_max         prix maximum EUR
year_min          annee minimum
year_max          annee maximum
mileage_min       kilometrage minimum
mileage_max       kilometrage maximum
zip               code postal 5 chiffres, ex: 27000 (requis pour distance)
distance_km       bucket UI distance (5 -> 200 km cote API)
distance_bucket   alias de distance_km (0 est valide)
good_deal         VERY_GOOD_DEAL, GOOD_DEAL, EQUITABLE_DEAL, BAD_DEAL
customer_family   PROFESSIONNEL, PARTICULIER, COURTIER_AUTOMOBILE, ...
energy            ELECTRIC, DIESEL, ESSENCE, HYBRID, ... (virgules OK)
gearbox           AUTO, MANUAL
body_type         CITADINE, SUV, BERLINE, ...
color             couleur exterieure
internal_color    couleur interieure
families          AUTO, UTILITY
options           equipements (virgules OK)
regions           regions (virgules OK)
equipment_level   niveau de finition
critair           Crit'Air max
co2_max           CO2 max
max_consumption   consommation max
doors             nombre de portes
power             puissance
seats             nombre de places
four_wheel        4x4 true/false
freetext          recherche libre
sort              newest, recent (alias), oldest, price_low, price_high, mileage_low
page              page, commence a 1 (defaut 1)
limit             1 a 72 (defaut 20)
url               URL listing La Centrale complete (passthrough filtres)
include_image     true/false (defaut false)
include_dealer    true/false (defaut false)
include_vehicle   true/false (defaut false)
debug             true/false
raw               true/false
```

Reponse wrapper :

```json
{
  "ok": true,
  "data": {
    "items": [],
    "pagination": {
      "page": 1,
      "limit": 10,
      "returned": 0,
      "max_fetchable": 72,
      "total": 24,
      "seed": 20260614123
    },
    "source": "json",
    "failures": []
  }
}
```

Champs item compacts :

- `id` : reference annonce
- `title`, `make`, `model`, `version`, `year`, `mileage`, `energy`, `gearbox`, `price`
- `location` : ville / departement / code postal
- `dealer_type` : famille vendeur (ex: `PROFESSIONNEL`, `PARTICULIER`)
- `good_deal_badge` : badge bonne affaire
- `url` : lien annonce La Centrale

Item compact typique :

```json
{
  "id": "B104008382",
  "title": "RENAULT ZOE (2) R110 INTENS 41KWH",
  "make": "RENAULT",
  "model": "ZOE",
  "version": "(2) R110 INTENS 41KWH",
  "year": 2019,
  "mileage": 95000,
  "energy": "ELECTRIC",
  "gearbox": "AUTO",
  "price": 7000,
  "currency": "EUR",
  "location": {"visitPlace": "95"},
  "dealer_type": "PROFESSIONNEL",
  "good_deal_badge": "EQUITABLE_DEAL",
  "url": "https://www.lacentrale.fr/auto-occasion-annonce-B104008382.html"
}
```

- `source` : `json` (API recherche), `ssr` (HTML listing), ou `unknown`
- `failures` : erreurs partielles par page (present seulement si non vide)
- Recherche sans resultat : `items: []` avec `ok: true` (pas d'erreur)

## Endpoint: Listing detail

```http
GET /listing/{ref}
```

`ref` = reference La Centrale (ex: `B104008382`).

Parametres :

```text
include_image       true/false (defaut true)
include_dealer      true/false (defaut false)
include_vehicle     true/false (defaut true)
include_description true/false (defaut false) — description, features, equipment
raw                 true/false (defaut false)
debug               true/false (defaut false)
```

Workflow recommande :

1. `/search` pour obtenir les `id` candidats
2. `/listing/{ref}` seulement pour les annonces a analyser en detail

```bash
curl -sS --connect-timeout 5 --max-time 90 \
  -H 'X-API-Key: <API_KEY>' \
  'http://127.0.0.1:8094/listing/B104008382?include_description=true'
```

Reponse :

```json
{
  "ok": true,
  "data": {
    "item": {
      "id": "B104008382",
      "title": "RENAULT ZOE ...",
      "price": 7000,
      "image": "https://...",
      "vehicle": {"make": "RENAULT", "model": "ZOE"},
      "description": "...",
      "features": "...",
      "equipment": "..."
    },
    "source": "json"
  }
}
```

- `source` : `json`, `html`, ou `json+html` (JSON enrichi par parse HTML)
- `include_description=true` peut declencher un fetch HTML si le JSON ne contient pas la description

## Endpoint: Price stats

```http
GET /price-stats
```

Memes filtres que `/search` (sans options d'affichage `include_*`, `debug`, `raw`). Defaut `limit=70`.

```bash
curl -sS --connect-timeout 5 --max-time 90 \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8094/price-stats' \
  --data-urlencode 'make=RENAULT' \
  --data-urlencode 'model=ZOE' \
  --data-urlencode 'price_max=8000' \
  --data-urlencode 'zip=27000' \
  --data-urlencode 'distance_km=5' \
  --data-urlencode 'limit=30'
```

Reponse :

```json
{
  "ok": true,
  "data": {
    "priced_count": 30,
    "count": 30,
    "sample_size": 30,
    "total": 41,
    "min": 2500,
    "max": 8000,
    "median": 6500,
    "mean": 6120,
    "p25": 5500,
    "p75": 7000,
    "currency": "EUR",
    "failures": []
  }
}
```

Les stats portent sur l'echantillon fetch, pas sur tout le marche (`total` = nombre upstream).

## Endpoint: Metadata

```http
GET /metadata
```

Auth requise. Parametres optionnels pour facettes scopees : `make`, `model`, `version`, `price_max`, `zip`, `distance_km`, `distance_bucket`, `energy`.

```bash
curl -sS --connect-timeout 5 --max-time 90 \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8094/metadata' \
  --data-urlencode 'make=RENAULT' \
  --data-urlencode 'model=ZOE'
```

Retourne sorts, badges, familles client, `distance_ui_to_api`, `page_size`, `max_limit`, `facets` (aggregations live), `total`.

## Endpoint: Health

```http
GET /health
```

Sans auth. Ne declenche pas de requete upstream.

```json
{
  "ok": true,
  "data": {
    "status": "up",
    "proxy_configured": true,
    "proxy_count": 1,
    "primary_strategy": "json",
    "datadome_configured": false,
    "upstream_api_key_configured": true,
    "max_fetchable_limit": 72
  }
}
```

## Endpoint: Warmup

```http
POST /warmup
```

Auth requise. **Operator-only** : bootstrap cookies DataDome sur www.lacentrale.fr. Ne pas appeler depuis un assistant conversationnel.

## Strategie recommandee achat auto

1. `/metadata` pour comprendre les filtres disponibles
2. `/search` avec filtres stricts (`energy`, `price_max`, `zip`, `distance_km`)
3. `/price-stats` sur le meme scope pour situer les prix
4. `/listing/{ref}` sur 2-3 candidats seulement
5. Ne pas promettre plus de 72 annonces par appel

## Limitations

- Proxy upstream requis pour fiabilite anti-bot.
- `distance_km=5` signifie bucket UI → `200km` API (voir metadata).
- Stats prix = echantillon visible, pas prix marche garanti.
- SSR fallback peut echouer : le service prefere JSON (`source: json`).
- `include_description` peut echouer si www.lacentrale.fr bloque le VPS (DataDome).
