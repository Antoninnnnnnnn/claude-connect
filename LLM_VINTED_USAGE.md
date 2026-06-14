# Instructions LLM - API Vinted

Hub APIs :

| API | Usage | Doc |
|-----|-------|-----|
| Vinted | mode, sneakers, revente | ce fichier |
| Leboncoin | petites annonces generalistes | `LLM_LEBONCOIN_USAGE.md` |
| La Centrale | voitures d'occasion | `LLM_CENTRALE_USAGE.md` |

Doc Leboncoin separee :

```text
LLM_LEBONCOIN_USAGE.md
```

Doc La Centrale separee :

```text
LLM_CENTRALE_USAGE.md
```

Utilise cette API HTTP pour chercher des annonces Vinted publiques et estimer des prix de revente.

## Auth

Ajoute toujours ce header :

```http
X-API-Key: <API_KEY>
```

## Base URL

Locale :

```text
http://127.0.0.1:8091
```

Via reverse proxy HTTPS (optionnel, pour Claude.ai ou un assistant web) :

```text
https://<your-domain>/vinted-api
```

## Regles D'utilisation

- Toutes les reponses sont en JSON.
- Une reponse reussie a toujours `{"ok": true, "data": ...}`.
- Une erreur a toujours `{"ok": false, "error": "..."}`.
- Si `ok` vaut `false`, ne devine pas le resultat : rapporte l'erreur.
- Par defaut, les resultats sont compacts pour economiser le contexte.
- N'utilise `raw=true` que si l'utilisateur demande explicitement le JSON brut ou un debug.
- N'utilise `include_photo=true` que si l'image est utile.
- N'utilise `include_seller=true` que si l'analyse du vendeur est utile.
- Ne pipe pas la reponse vers `python3 -m json.tool` ou `jq` sauf debug humain : le pretty-print consomme plus de contexte.
- Utilise `curl -sS -G --data-urlencode` pour encoder proprement les recherches avec espaces, accents ou caracteres speciaux.
- Ajoute `--connect-timeout 5 --max-time 30` pour eviter de rester bloque.

## Curl Recommande

Forme recommandee pour un LLM :

```bash
curl -sS --connect-timeout 5 --max-time 30 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8091/search' \
  --data-urlencode 'query=nike tn' \
  --data-urlencode 'domain=fr' \
  --data-urlencode 'order=newest' \
  --data-urlencode 'per_page=5'
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
query       texte recherche, ex: nike tn, carhartt jacket
brand       nom de marque ou ID Vinted; ID numerique = filtre exact
size        ID taille Vinted, ou plusieurs IDs separes par virgule
condition   new, new_without_tags, very_good, good, satisfactory, ou ID Vinted
price_min   prix minimum
price_max   prix maximum
catalog     ID categorie Vinted, ou plusieurs IDs separes par virgule
order       newest, relevance, price_low, price_high
domain      fr, de, it, es
per_page    1 a 96
page        page, commence a 1
include_photo   true/false
include_seller  true/false
raw             true/false
```

Exemple compact :

```bash
curl -sS --connect-timeout 5 --max-time 30 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8091/search' \
  --data-urlencode 'query=nike tn' \
  --data-urlencode 'price_max=60' \
  --data-urlencode 'domain=fr' \
  --data-urlencode 'order=newest' \
  --data-urlencode 'per_page=10'
```

Champs retour par defaut :

```json
{
  "id": 9078421757,
  "title": "Nike Track Jacket...",
  "brand": "Nike",
  "size": "L / 40 / 12",
  "condition": "Neuf sans etiquette",
  "price": 16.95,
  "currency": "EUR",
  "total_item_price": 18.5,
  "url": "https://www.vinted.fr/items/..."
}
```

Avec image et vendeur compact :

```bash
curl -sS --connect-timeout 5 --max-time 30 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8091/search' \
  --data-urlencode 'query=nike tn' \
  --data-urlencode 'price_max=60' \
  --data-urlencode 'domain=fr' \
  --data-urlencode 'per_page=5' \
  --data-urlencode 'include_photo=true' \
  --data-urlencode 'include_seller=true'
```

## Endpoint: Item

Obtenir les infos d'une annonce.

```http
GET /item/{id}
```

Parametres :

```text
domain          fr, de, it, es
include_photo   true/false
include_seller  true/false
raw             true/false
```

Utilisation recommandee :

1. Appelle d'abord `/search`.
2. Recupere un `id`.
3. Appelle `/item/{id}` seulement si tu as besoin de confirmer ou enrichir l'annonce.

Exemple :

```bash
curl -sS --connect-timeout 5 --max-time 30 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8091/item/9078421757' \
  --data-urlencode 'domain=fr'
```

Le retour doit contenir `details_available: true`. La source normale peut etre `details_api` ou `item_page`.

Champs utiles par defaut :

```text
id, title, brand, brand_id, size, condition, description,
category, category_leaf, categories, category_ids, color,
price, currency, total_item_price, shipping_price, shipping_text,
upload_date, availability, status_schema, service_fee_included, url
```

Avec `include_photo=true`, ajoute `photo` et `photos`.

Avec `include_seller=true`, ajoute un vendeur compact :

```text
seller.id, seller.login, seller.profile_url, seller.location,
seller.last_seen, seller.rating si disponible
```

Si `details_available` vaut `false` ou si `ok` vaut `false`, considere que le detail n'a pas ete recupere correctement.

## Endpoint: Price Stats

Calculer une statistique de prix sur les annonces actuellement visibles.

```http
GET /price-stats
```

Parametres : memes filtres principaux que `/search`.

Retour :

```json
{
  "count": 10,
  "min": 5.0,
  "max": 30.0,
  "median": 10.0,
  "currency": "EUR",
  "sample_size": 10
}
```

Exemple :

```bash
curl -sS --connect-timeout 5 --max-time 30 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8091/price-stats' \
  --data-urlencode 'query=nike tn' \
  --data-urlencode 'price_max=120' \
  --data-urlencode 'domain=fr' \
  --data-urlencode 'per_page=50'
```

## Strategie Recommandee Pour Le Resell

1. Utilise `/search` avec `order=newest` pour trouver les nouvelles annonces.
2. Compare `price` ou `total_item_price` avec `/price-stats`.
3. Considere une annonce interessante si son prix est nettement sous la mediane.
4. Utilise `include_photo=true` seulement quand l'image aide a identifier le modele.
5. Utilise `include_seller=true` seulement si tu veux verifier le vendeur.
6. Ne presente jamais `/price-stats` comme un prix de vente garanti : c'est une estimation basee sur les annonces actives visibles.
