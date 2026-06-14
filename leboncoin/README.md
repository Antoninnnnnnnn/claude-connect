# Self-hosted Leboncoin Search API

Mini-API FastAPI qui expose la recherche publique Leboncoin en JSON.

La strategie principale lit le JSON `__NEXT_DATA__` des pages publiques Leboncoin. Le client `lbc` de base a ete conserve comme reference, mais les endpoints API Leboncoin directs sont souvent bloques par DataDome sur ce VPS.

## Configuration

Le fichier sensible commun reste a la racine du depot :

```bash
../.env
```

Variables utiles :

- `API_KEY` : cle attendue dans le header `X-API-Key`.
- `LBC_PORT` : port d'ecoute, defaut `8092`.
- `LBC_PROXY` : proxy HTTP(S) residentiel unique.
- `LBC_PROXIES` : plusieurs proxys separes par virgule ou point-virgule.
- `DECODO_PROXY`, `DATAIMPULSE_PROXY`, `EVOMI_PROXY` : proxys dedies possibles.
- `VINTED_PROXY` : fallback si aucun proxy Leboncoin dedie n'est defini.
- `LBC_MIN_INTERVAL` : delai minimal entre deux appels Leboncoin, defaut `1.5`.
- `LBC_MAX_RETRIES` : retries par page, defaut `3`.
- `LBC_TIMEOUT` : timeout par appel, defaut `45`.
- `LBC_CACHE_TTL` : cache court du JSON Leboncoin par URL, defaut `20` secondes.
- `LBC_CACHE_MAX_ENTRIES` : taille max du cache court, defaut `256`.

Doc LLM operationnelle : [`LLM_LEBONCOIN_USAGE.md`](../LLM_LEBONCOIN_USAGE.md)

## Endpoints

Toutes les reponses suivent `{"ok": true, "data": ...}` ou `{"ok": false, "error": "..."}`.

- `GET /search`
- `GET /ad/{id}`
- `GET /price-stats`
- `GET /metadata`
- `GET /docs`

## Exemples

```bash
curl -H "X-API-Key: $API_KEY" \
"http://127.0.0.1:8092/search?text=nike&category=sneakers&price_max=80&sort=newest&limit=5"
```

Reponse compacte par defaut : pas d'image URL, pas de description, pas de vendeur complet, pas d'attributs bruts, pas de raw. Les champs enrichis compacts sont inclus quand disponibles : `brand`, `condition`, `old_price`, `image_count`, `shipping`, `seller_rating`, `options`, `category_key`, `category_path`.

Options opt-in utiles :

- `include_image=true` : ajoute seulement l'image principale.
- `include_images=true` : ajoute toutes les images.
- `include_body=true` : ajoute la description.
- `include_owner=true` : ajoute le vendeur compact.
- `include_attributes=true` : ajoute les attributs Leboncoin.
- `include_attributes=true` ajoute aussi `attributes_map` pour acceder directement aux cles Leboncoin.
- `include_coordinates=true` : ajoute `lat/lng` dans la localisation.
- `debug=true` : ajoute source/failures de diagnostic.
- `raw=true` : ajoute le payload brut, tres verbeux.

```bash
curl -H "X-API-Key: $API_KEY" \
"http://127.0.0.1:8092/search?text=iphone&category=telephone&location=dept:75&price_max=300&limit=10"
```

```bash
curl -H "X-API-Key: $API_KEY" \
"http://127.0.0.1:8092/ad/2883384910"
```

```bash
curl -H "X-API-Key: $API_KEY" \
"http://127.0.0.1:8092/price-stats?text=nike&category=sneakers&price_max=80&limit=70"
```

## Test de resistance

```bash
python scripts_stress_test.py --count 10 --delay 2 --text nike --category sneakers --limit 5
```
