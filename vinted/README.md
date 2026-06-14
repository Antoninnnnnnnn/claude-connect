# Self-hosted Vinted Search API

Mini-API FastAPI qui expose la recherche publique Vinted en JSON.

## Configuration

La configuration sensible est centralisee a la racine du depot :

```bash
../.env
```

Variables utiles :

- `API_KEY` : clé attendue dans le header `X-API-Key`.
- `VINTED_PORT` : port d'écoute, défaut `8091`.
- `VINTED_DEFAULT_DOMAIN` : `fr`, `de`, `it` ou `es`.
- `VINTED_PROXY` : proxy HTTP(S) résidentiel optionnel.
- `VINTED_MIN_INTERVAL` : délai minimal entre deux appels Vinted.
- `VINTED_COOKIE_FILE` : fichier de persistance des cookies publics Vinted.

## Lancer

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8091
```

Docs OpenAPI : `/docs`.

En production locale, le service est lance par systemd utilisateur :

```bash
systemctl --user status vinted-api.service
systemctl --user restart vinted-api.service
```

## Endpoints

Toutes les réponses suivent `{"ok": true, "data": ...}` ou `{"ok": false, "error": "..."}`.

- `GET /search`
- `GET /item/{id}`
- `GET /price-stats`

Les reponses sont compactes par defaut. Options utiles :

- `include_photo=true`
- `include_seller=true`
- `raw=true`
