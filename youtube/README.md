# Self-hosted YouTube Transcript API

Mini-API FastAPI qui renvoie les sous-titres d'une video YouTube en JSON compact, pour qu'un agent LLM puisse lire, resumer ou citer une video.

S'appuie sur [`youtube-transcript-api`](https://github.com/jdepoix/youtube-transcript-api), sans cle Google ni navigateur.

Points importants :

- **IP de datacenter bloquees** : YouTube refuse les sous-titres a la plupart des IP cloud (`RequestBlocked` / `IpBlocked`). Sur un VPS, configurer un proxy residentiel (`YT_PROXY`, `YT_PROXIES`, ou les proxys partages `DECODO_PROXY`, `DATAIMPULSE_PROXY`, `EVOMI_PROXY`). Sans proxy, le client sort en direct.
- **Une tentative = une nouvelle session** : la librairie n'est pas thread-safe, et une nouvelle connexion a une passerelle residentielle rotative donne une nouvelle IP de sortie. Seules les erreurs de blocage ou reseau sont rejouees ; video indisponible, sous-titres desactives ou age-restricted echouent tout de suite (une autre IP n'y changerait rien).
- **Timeouts** : la librairie n'en pose aucun. `YT_TIMEOUT` borne chaque requete HTTP, `YT_DEADLINE` l'appel complet retries compris (garder sous le `proxy_read_timeout` nginx de 90s).
- **Chemin leger (`YT_LIGHT_MODE`, actif par defaut)** : la librairie telecharge la page watch (~300 KB) juste pour y lire une cle, puis l'API player (~50 KB). [`app/light.py`](app/light.py) saute la page watch et demande a l'API player seulement 3 champs (`X-Goog-FieldMask`) : 1-2 KB. Mesure : **1-12 KB par transcription non cachee** au lieu de ~360-400 KB, sous-titres compris. Une tentative bloquee coute aussi ~2 KB au lieu de ~300 KB.
  - Repose sur des internes prives de la version epinglee de la librairie : re-tester avant de la monter.
  - Si YouTube refuse la forme de la requete (4xx hors 429, JSON inattendu), repli automatique sur le chemin complet de la librairie dans la meme tentative : ca coute de la bande passante, pas la disponibilite. Le log indique `served via light|full path`.
  - Non verifie depuis une IP de datacenter : si les blocages augmentent, comparer avec `YT_LIGHT_MODE=false`.
- **Cache** : les transcriptions ne changent pas, cache long (`YT_CACHE_TTL`, 6h) indexe par piste. La pagination (`start`, `max_chars`) relit le cache au lieu de refaire l'appel.
- **Pas de traduction** : l'endpoint de traduction YouTube (`tlang`) renvoie 429 meme quand le reste passe. L'agent traduit lui-meme.
- **Messages d'erreur courts** : les exceptions de la librairie (plusieurs paragraphes, liens GitHub) sont remplacees par un message court et un `error_code`.

## Configuration

Le fichier sensible commun reste a la racine du depot :

```bash
../.env
```

Variables utiles :

- `API_KEY` : cle attendue dans le header `X-API-Key`.
- `YT_PORT` : port d'ecoute, defaut `8095`.
- `YT_PROXY`, `YT_PROXIES` : proxy(s) residentiel(s), separes par virgule ou point-virgule.
- `DECODO_PROXY`, `DATAIMPULSE_PROXY`, `EVOMI_PROXY` : proxys partages, ajoutes au pool.
- `YT_DIRECT_FIRST` : essaie l'IP du serveur avant le pool, defaut `false`. Utile si l'IP du VPS passe : economise la bande passante residentielle.
- `YT_ALLOW_DIRECT_FALLBACK` : essaie l'IP du serveur apres echec du pool, defaut `false`.
- `YT_MAX_RETRIES` : tentatives via le pool, defaut `4`.
- `YT_TIMEOUT` : timeout par requete HTTP, defaut `15`.
- `YT_DEADLINE` : budget total d'un appel, defaut `75` secondes.
- `YT_MIN_INTERVAL` : delai minimal entre deux tentatives, defaut `0.5`.
- `YT_DEFAULT_LANGUAGES` : langues preferees quand `lang` est absent, defaut `fr,en`.
- `YT_DEFAULT_MAX_CHARS` : taille max de sortie par defaut, `20000` caracteres (~5k tokens).
- `YT_LIGHT_MODE` : chemin leger via l'API player masquee, defaut `true`.
- `YT_FETCH_TITLE` : titre et chaine via oEmbed quand le chemin leger ne les fournit pas (repli), defaut `true`.
- `YT_CACHE_TTL` : cache des transcriptions, defaut `21600` secondes.
- `YT_CACHE_MAX_ENTRIES` : taille max du cache, defaut `64`.

Doc LLM operationnelle : [`LLM_YOUTUBE_USAGE.md`](../LLM_YOUTUBE_USAGE.md)

## Lancer

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
set -a && source ../.env && set +a
uvicorn app.main:app --host 127.0.0.1 --port 8095
```

Docs OpenAPI : `/docs`.

## Endpoints

Toutes les reponses suivent `{"ok": true, "data": ...}` ou `{"ok": false, "error": "...", "error_code": "..."}`.

- `GET /health` : public, etat du service et du pool de proxys.
- `GET /transcript?video=<id ou URL>` : sous-titres. Parametres : `lang`, `strict`, `format` (`text` | `segments`), `start`, `end`, `max_chars`, `paragraph_seconds`, `include_languages`.
- `GET /languages?video=<id ou URL>` : pistes de sous-titres disponibles.

Codes d'erreur : `invalid_video_id` (422), `video_unavailable` (404), `no_transcripts` (404), `language_not_found` (404, avec `available_languages`), `age_restricted` (422), `video_unplayable` (422), `blocked` (502), `po_token_required` (502), `network_error` (502), `upstream_error` (502).

## Tests

```bash
.venv/bin/pip install -r ../requirements-dev.txt
.venv/bin/pytest
```

Tests hors ligne : l'API YouTube est remplacee par une fausse fabrique, aucun appel reseau.
