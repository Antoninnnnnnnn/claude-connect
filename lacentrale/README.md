# Self-hosted La Centrale Search API

Mini-API FastAPI qui expose la recherche automobile La Centrale en JSON compact pour les LLMs.

## Strategie upstream

- **Primaire** : `GET recherche.lacentrale.fr/v5/search` (session persistante + prime aggregations)
- **Fallback** : SSR `www.lacentrale.fr/listing?...` (si JSON indisponible)
- **Detail** : `GET recherche.lacentrale.fr/v5/search?references={ref}` puis fallback HTML (Apollo / `CLASSIFIED_MAIN_INFOS` / `SummaryInformationData`, inspire de [ummhensi/lacentrale-scraper](https://github.com/ummhensi/lacentrale-scraper))
- **Transport** : `curl_cffi` + proxy residentiel (meme pool que Vinted/Leboncoin)
- **HTML www** : clearance DataDome mintee par camoufox headed (`app/mint_datadome.py`) puis rejouee par `curl_cffi` (`app/www_session.py`)

### Venv navigateur (mint DataDome)

```bash
python3 -m venv ../.venv-browser
../.venv-browser/bin/pip install camoufox[geoip]
../.venv-browser/bin/python -m camoufox fetch
sudo apt install -y xvfb   # le device check ne se resout qu'en mode headed
```


## Reponse API

- Succes : `{"ok": true, "data": ...}`
- Erreur : `{"ok": false, "error": "..."}` (401, 404, 422, 502, 500)

## Configuration

Fichier sensible commun a la racine du depot :

```bash
../.env
```

Variables utiles :

| Variable | Defaut | Role |
|----------|--------|------|
| `API_KEY` | — | cle attendue dans le header `X-API-Key` |
| `CENTRALE_PORT` | `8094` | port d'ecoute |
| `CENTRALE_HOST` | `0.0.0.0` | bind address |
| `CENTRALE_PROXY` / `CENTRALE_PROXIES` | — | proxy dedie ou pool |
| `CENTRALE_UPSTREAM_API_KEY` | auto JS | cle front La Centrale |
| `CENTRALE_CLIENT_SOURCE` | `lc:recherche:front` | header upstream |
| `CENTRALE_PRIMARY_STRATEGY` | `auto` | `auto`, `json`, `ssr` |
| `CENTRALE_TIMEOUT` | `45` | timeout requetes |
| `CENTRALE_MIN_INTERVAL` | `1.5` | throttle entre requetes |
| `CENTRALE_MAX_RETRIES` | `3` | retries anti-bot |
| `CENTRALE_MAX_PAGES_PER_SEARCH` | `3` | pages fetch (24/page) |
| `CENTRALE_CACHE_TTL` | `20` | cache JSON recherche |
| `CENTRALE_METADATA_CACHE_TTL` | `300` | cache facettes |
| `CENTRALE_COOKIE_FILE` | `data/cookies.json` | cookies www (sans JWT) |
| `CENTRALE_WWW_USE_PROXY` | `true` | proxy sur pages www |
| `CENTRALE_WARMUP_ON_START` | `false` | warmup async au demarrage (desactive par defaut) |
| `CENTRALE_IMPERSONATES` | `chrome_android,chrome131_android` | fingerprints TLS (DataDome bloque les desktop sur l'API JSON) |
| Pool partage | `VINTED_PROXY`, `LBC_PROXIES`, `DECODO_PROXY`, etc. | fallback proxies |

Acces HTML `www.lacentrale.fr` (clearance DataDome mintee par navigateur, cf. `app/www_session.py`) :

| Variable | Defaut | Role |
|----------|--------|------|
| `CENTRALE_BROWSER_ENABLED` | `true` | active le mint camoufox |
| `CENTRALE_BROWSER_PYTHON` | `../.venv-browser/bin/python` | venv qui contient camoufox |
| `CENTRALE_BROWSER_XVFB` | `true` | wrap `xvfb-run -a` (le device check ne se resout qu'en mode headed) |
| `CENTRALE_BROWSER_PROXY` | `CENTRALE_PROXY` | exit residentiel FR pour le mint |
| `CENTRALE_BROWSER_MINT_ATTEMPTS` | `4` | retries navigateur (nouvel IP a chaque essai) |
| `CENTRALE_BROWSER_MINT_TIMEOUT` | `420` | timeout du subprocess de mint |
| `CENTRALE_BROWSER_MINT_COOLDOWN` | `300` | pause apres un mint rate |
| `CENTRALE_DATADOME_TOKEN_FILE` | `data/datadome_token.json` | persistance du cookie (gitignore, chmod 600) |
| `CENTRALE_DATADOME_TOKEN_MAX_AGE` | `3600` | re-mint preventif |
| `CENTRALE_WWW_MIN_INTERVAL` | `5` | espacement des fetch www (la clearance grille a ~1 req/s) |
| `CENTRALE_WWW_IMPERSONATE` | `chrome` | fingerprint des fetch www |
| `CENTRALE_WWW_FETCH_USE_PROXY` | `false` | le cookie est portable d'IP, pas besoin du proxy metre |

Si `CENTRALE_UPSTREAM_API_KEY` est vide, la cle est extraite du bundle JS listing (decouverte dynamique du hash).

Doc LLM : [`LLM_CENTRALE_USAGE.md`](../LLM_CENTRALE_USAGE.md)

## Endpoints

| Route | Auth | Description |
|-------|------|-------------|
| `GET /health` | non | statut service + proxy + strategy |
| `POST /warmup` | oui | bootstrap cookies datadome |
| `GET /search` | oui | recherche voitures |
| `GET /listing/{ref}` | oui | detail annonce (`W102941021`) |
| `GET /price-stats` | oui | min/max/median sur echantillon |
| `GET /metadata` | oui | facettes + listes statiques |
| `GET /docs` | non | OpenAPI |

## Filtres `/search`

Core : `make`, `model`, `version`, `price_min/max`, `year_min/max`, `mileage_min/max`, `zip`, `distance_km` (bucket UI, pas des km litteraux), `good_deal`, `customer_family`, `sort`, `page`, `limit`, `url`.

Avances : `energy`, `gearbox`, `body_type`, `color`, `internal_color`, `families`, `options`, `regions`, `equipment_level`, `critair`, `co2_max`, `max_consumption`, `doors`, `power`, `seats`, `four_wheel`, `freetext`.

Alias : `distance_bucket` = `distance_km`.

Limite effective : `24 x CENTRALE_MAX_PAGES_PER_SEARCH` (defaut **72**).

## Options opt-in

- `/search` : `include_image`, `include_dealer`, `include_vehicle`, `debug`, `raw` (defaut compact)
- `/listing/{ref}` : `include_image=true`, `include_vehicle=true` par defaut

## Lancement manuel

```bash
cd lacentrale
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
set -a && source ../.env && set +a
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8094
```

## Service systemd (user)

```bash
mkdir -p ~/.config/systemd/user
cp lacentrale-api.service.example ~/.config/systemd/user/lacentrale-api.service
systemctl --user daemon-reload
systemctl --user enable --now lacentrale-api.service
systemctl --user restart lacentrale-api.service
systemctl --user status lacentrale-api.service
```

## Probe upstream (Phase 0)

```bash
.venv/bin/python scripts/upstream_probe.py
```

## Exemples

```bash
curl -sS -H "X-API-Key: $API_KEY" http://127.0.0.1:8094/health

curl -sS -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:8094/search?make=RENAULT&model=ZOE&version=intens&price_max=8000&zip=27000&distance_km=5&energy=ELECTRIC&limit=5"

curl -sS -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:8094/listing/B104008382"

curl -sS -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:8094/price-stats?make=RENAULT&model=ZOE&price_max=8000&zip=27000&limit=30"

curl -sS -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:8094/metadata?make=RENAULT&model=ZOE"
```

## Limitations connues

- Proxy residentiel recommande (DataDome).
- `distance_km=5` correspond au bucket UI → `200km` cote API (voir `/metadata`).
- HTML www : DataDome exige une clearance mintee par navigateur (camoufox headed sous xvfb, ~25 s). Le premier appel a `include_description=true` ou a la strategie `ssr` paie ce mint, ensuite le cookie est reutilise. `CENTRALE_PRIMARY_STRATEGY=json` reste le defaut recommande (l'API JSON est plus rapide et sans mint).
- `description` absente sur beaucoup d'annonces pro : la page renvoie `description: {}` ou `{"status":"REFUSED"}` — donnee vraiment absente, pas un blocage.
- Les stats prix portent sur l'echantillon fetch, pas le marche entier.
- Recherche sans resultat retourne `items: []` (pas d'erreur).
