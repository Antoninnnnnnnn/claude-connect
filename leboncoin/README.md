# Self-hosted Leboncoin Search API

Mini-API FastAPI qui expose la recherche publique Leboncoin en JSON.

La strategie principale interroge l'API JSON officielle de l'app mobile :
`POST https://api.leboncoin.fr/finder/search` pour la recherche et
`GET https://api.leboncoin.fr/api/adfinder/v1/classified/{id}` pour une annonce.
Le scraping de `__NEXT_DATA__` sur les pages HTML a ete abandonne (une page pesait ~400 KB
pour le meme resultat).

Points importants :

- **Empreinte mobile obligatoire** : DataDome renvoie 403 aux profils TLS desktop. `LBC_IMPERSONATES` doit rester sur des profils mobiles (`safari_ios`, `chrome_android`, `firefox`).
- **Cookie DataDome** : l'endpoint recherche refuse (403) une session porteuse d'un cookie DataDome, l'endpoint annonce l'exige. Le client ne prime donc la session que pour `/ad/{id}`, via un POST `finder/search` a 1 resultat (~8 KB) au lieu de la page d'accueil (~400 KB).
- **Proxy optionnel** : l'acces direct depuis l'IP du serveur passe la verification DataDome. Les proxys residentiels ne servent plus que de fallback (leurs IP de sortie sont souvent bridees a quelques KB/s).
- **Sessions chaudes** : la session curl_cffi et son cookie sont reutilises pendant `LBC_SESSION_TTL` secondes, ce qui amortit le priming a ~0.

## Configuration

Le fichier sensible commun reste a la racine du depot :

```bash
../.env
```

Variables utiles :

- `API_KEY` : cle attendue dans le header `X-API-Key`.
- `LBC_PORT` : port d'ecoute, defaut `8092`.
- `LBC_PROXY` : proxy HTTP(S) residentiel unique (optionnel, fallback uniquement).
- `LBC_PROXIES` : plusieurs proxys separes par virgule ou point-virgule.
- `DECODO_PROXY`, `DATAIMPULSE_PROXY`, `EVOMI_PROXY` : proxys dedies possibles.
- `VINTED_PROXY` : fallback si aucun proxy Leboncoin dedie n'est defini.
- `LBC_IMPERSONATES` : empreintes TLS, defaut `safari_ios,chrome_android,firefox` (garder du mobile).
- `LBC_SESSION_TTL` : duree de reutilisation d'une session chaude, defaut `600` secondes.
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
