# Self-hosted YouTube Transcript API

Mini-API FastAPI qui renvoie les sous-titres d'une video YouTube en JSON compact, pour qu'un agent LLM puisse lire, resumer ou citer une video. Elle cherche aussi des videos, liste les videos d'une chaine ou d'une playlist, et donne les metadonnees d'une video.

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
- **Recherche, chaines, playlists, metadonnees** ([`app/innertube.py`](app/innertube.py)) : l'API JSON interne de youtube.com (`/youtubei/v1/search`, `/browse`, `/navigation/resolve_url`, `/player`, client WEB), sans cle. Ce n'est pas une API de donnees : YouTube renomme ses renderers sans prevenir (migration `videoRenderer` vers `lockupViewModel` a moitie faite en 2026-09). Le parseur ignore ce qu'il ne connait pas : un changement donne moins de champs ou d'items, jamais une erreur. Les canaris `tests_live` le detectent.
  - Vues, dates relatives et durees sont renvoyees telles qu'affichees (`"2.2M views"`, `"6 days ago"`) : les parser casserait a chaque changement de libelle. `/video` donne les nombres et la date exacte.
  - Pagination : `next` encode la page YouTube et le nombre d'items deja servis, donc `limit` peut couper une page sans rien perdre.
  - Tri par date d'upload en recherche : YouTube l'ignore depuis 2025, remplace par le filtre `upload`.
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
- `YT_WEB_CLIENT_VERSION` : version du client WEB innertube, defaut `2.20250925.01.00`. A monter si recherche/chaine repondent `upstream_rejected`.
- `YT_HL`, `YT_GL` : langue d'interface et pays de classement, defaut `fr` / `FR`. YouTube traduit les titres dans la langue d'interface quand une traduction existe : un titre peut differer de l'original.
- `YT_BROWSE_CACHE_TTL`, `YT_BROWSE_CACHE_MAX_ENTRIES` : cache des pages de resultats, `600` s et `128` entrees, separe des transcriptions.
- `YT_MAX_PAGES` : pages YouTube max par appel pour atteindre `limit`, defaut `5`.

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
- `GET /search?q=` : recherche. `type` (`video` defaut, `channel`, `playlist`, `all`), `duration` (`short` <4 min, `medium` 4-20, `long` >20), `upload` (`hour` a `year`), `sort` (`relevance`, `views`), `limit`, `next`.
- `GET /channel?channel=<@handle, UC..., URL>` : infos de la chaine et ses videos. `tab` (`videos`, `shorts`, `streams`, `playlists`), `sort` (`latest`, `popular`, `oldest`), `limit`, `next`.
- `GET /playlist?playlist=<PL... ou URL avec list=>` : videos d'une playlist. `limit`, `next`.
- `GET /video?video=<id ou URL>` : metadonnees (date de publication exacte, vues, duree, description, mots-cles).

Codes d'erreur : `invalid_reference` (422), `channel_not_found` (404), `playlist_not_found` (404), `sort_unavailable` (422), `upstream_rejected` (502), `invalid_video_id` (422), `video_unavailable` (404), `no_transcripts` (404), `language_not_found` (404, avec `available_languages`), `age_restricted` (422), `video_unplayable` (422), `blocked` (502), `po_token_required` (502), `network_error` (502), `upstream_error` (502).

## Tests

```bash
.venv/bin/pip install -r ../requirements-dev.txt
.venv/bin/pytest
```

Tests hors ligne : l'API YouTube est remplacee par une fausse fabrique, aucun appel reseau.
