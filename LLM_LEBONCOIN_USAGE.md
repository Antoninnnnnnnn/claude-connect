# Instructions LLM - API Leboncoin

Utilise cette API HTTP pour chercher des annonces Leboncoin publiques, lire une annonce, et estimer des prix.

## Auth

Ajoute toujours ce header :

```http
X-API-Key: <API_KEY>
```

## Base URL

Locale :

```text
http://127.0.0.1:8092
```

Via reverse proxy HTTPS (optionnel) :

```text
https://<your-domain>/leboncoin-api
```

## Regles D'utilisation

- Toutes les reponses sont en JSON.
- Une reponse reussie a toujours `{"ok": true, "data": ...}`.
- Une erreur a toujours `{"ok": false, "error": "..."}`.
- Si `ok` vaut `false`, ne devine pas le resultat : rapporte l'erreur.
- Par defaut, les resultats sont compacts pour economiser le contexte.
- Utilise `curl -sS -G --data-urlencode` pour encoder proprement les recherches avec espaces, accents ou caracteres speciaux.
- Ajoute `--connect-timeout 5 --max-time 60` pour eviter de rester bloque.
- Ne pipe pas la reponse vers `python3 -m json.tool` ou `jq` sauf debug humain.
- N'utilise `raw=true` que si l'utilisateur demande explicitement le JSON brut ou un debug.
- N'utilise les champs optionnels que quand ils servent l'analyse.
- Utilise `/metadata` si tu as besoin de connaitre les categories, alias, sorts ou formats de localisation disponibles.

## Curl Recommande

Forme recommandee pour un LLM :

```bash
curl -sS --connect-timeout 5 --max-time 60 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8092/search' \
  --data-urlencode 'text=nike tn' \
  --data-urlencode 'category=sneakers' \
  --data-urlencode 'price_max=80' \
  --data-urlencode 'sort=newest' \
  --data-urlencode 'limit=10'
```

Ne fais pas ceci par defaut :

```bash
curl ... | python3 -m json.tool
```

Le JSON compact est plus facile a reutiliser dans le contexte d'un LLM.

## Endpoint: Search

Chercher des annonces.

```http
GET /search
```

Parametres utiles :

```text
text       texte recherche, ex: nike tn, iphone 13, carhartt jacket
category   ID categorie Leboncoin ou alias: sneakers, chaussures, mode, telephone, velos
location   dept:75, region:11, paris, ile_de_france, d_75, r_11, ou lat,lng,radius
lat         latitude si recherche geographique
lng         longitude si recherche geographique
radius      rayon en metres si lat/lng, entre 1000 et 200000
price_min   prix minimum
price_max   prix maximum
sort        newest, oldest, relevance, price_low, price_high
page        page, commence a 1
limit       1 a 105, recommande 5 a 20
url         URL complete de recherche Leboncoin; remplace les autres filtres
include_image        true/false, image principale seulement
include_images       true/false, toutes les images, verbeux
include_body         true/false, description, verbeux sur search
include_owner        true/false, vendeur compact
include_attributes   true/false, attributs Leboncoin, verbeux
include_coordinates  true/false, lat/lng dans location
debug                true/false, diagnostics source/failures
raw                  true/false, payload brut, tres verbeux
```

Exemple compact :

```bash
curl -sS --connect-timeout 5 --max-time 60 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8092/search' \
  --data-urlencode 'text=nike tn' \
  --data-urlencode 'category=sneakers' \
  --data-urlencode 'price_max=80' \
  --data-urlencode 'sort=newest' \
  --data-urlencode 'limit=10'
```

Exemple avec localisation :

```bash
curl -sS --connect-timeout 5 --max-time 60 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8092/search' \
  --data-urlencode 'text=iphone 13' \
  --data-urlencode 'category=telephone' \
  --data-urlencode 'location=dept:75' \
  --data-urlencode 'price_max=350' \
  --data-urlencode 'limit=10'
```

Champs retour par defaut :

```json
{
  "id": 2883384910,
  "title": "Baskets Nike Sportwear Valiant Unisex",
  "brand": "Nike",
  "category_id": "53",
  "category_name": "Chaussures",
  "category_key": "mode_chaussures",
  "category_path": ["Mode", "Chaussures"],
  "ad_type": "offer",
  "status": "active",
  "condition": "Tres bon etat",
  "price": 35.0,
  "old_price": 75.0,
  "currency": "EUR",
  "url": "https://www.leboncoin.fr/ad/chaussures/2883384910",
  "image_count": 5,
  "first_publication_date": "2024-11-15 12:05:18",
  "index_date": "2026-06-02 23:59:38",
  "location": {
    "region_name": "Ile-de-France",
    "department_name": "Paris",
    "city_label": "Paris 75010 10e Arrondissement",
    "city": "Paris",
    "zipcode": "75010"
  },
  "seller_rating": {"score": 1.0, "count": 111},
  "shipping": {
    "shippable": true,
    "methods": ["mondial_relay", "shop2shop", "colissimo", "face_to_face"],
    "parcel_size": "S",
    "parcel_weight_g": 100,
    "bundleable": true,
    "purchase_available": true,
    "negotiation_available": true
  },
  "options": {"gallery": true, "is_boosted": true}
}
```

Si tu as besoin de tous les attributs Leboncoin exploitables sans le payload brut, ajoute `include_attributes=true`. Utilise alors `attributes_map` pour lire directement une cle comme `condition`, `shoe_size`, `shoe_brand`, `shipping_type`, `rating_score`, etc.

## Endpoint: Metadata

Lister les categories, alias et formats disponibles.

```http
GET /metadata
```

Exemple :

```bash
curl -sS --connect-timeout 5 --max-time 30 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  'http://127.0.0.1:8092/metadata'
```

## Endpoint: Ad

Obtenir les infos d'une annonce precise.

```http
GET /ad/{id}
```

Parametres utiles :

```text
include_image        true/false
include_images       true/false
include_body         true/false, active par defaut sur /ad
include_owner        true/false
include_attributes   true/false
include_coordinates  true/false
debug                true/false
raw                  true/false
```

Utilisation recommandee :

1. Appelle d'abord `/search`.
2. Recupere un `id`.
3. Appelle `/ad/{id}` seulement si tu as besoin de la description, d'images, du vendeur ou d'attributs.

Exemple :

```bash
curl -sS --connect-timeout 5 --max-time 60 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8092/ad/2883384910' \
  --data-urlencode 'include_image=true' \
  --data-urlencode 'include_owner=true'
```

## Endpoint: Price Stats

Calculer une statistique de prix sur les annonces actuellement visibles.

```http
GET /price-stats
```

Parametres : memes filtres principaux que `/search`.

Retour :

```json
{
  "count": 35,
  "min": 1.0,
  "max": 80.0,
  "median": 45.0,
  "currency": "EUR",
  "sample_size": 35,
  "failures": []
}
```

Exemple :

```bash
curl -sS --connect-timeout 5 --max-time 60 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8092/price-stats' \
  --data-urlencode 'text=nike tn' \
  --data-urlencode 'category=sneakers' \
  --data-urlencode 'price_max=120' \
  --data-urlencode 'limit=70'
```

## Strategie Recommandee Pour Le Resell

1. Utilise `/search` avec `sort=newest` pour trouver les nouvelles annonces.
2. Utilise `/price-stats` avec les memes filtres pour obtenir une mediane.
3. Considere une annonce interessante si son prix est nettement sous la mediane et si le titre/localisation sont coherents.
4. Appelle `/ad/{id}` seulement pour les annonces candidates.
5. Active `include_image=true` seulement quand l'image aide a identifier le modele.
6. Active `include_owner=true` seulement si l'analyse vendeur est utile.
7. Ne presente jamais `/price-stats` comme un prix de vente garanti : c'est une estimation basee sur les annonces actives visibles.
