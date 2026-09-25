# Instructions LLM - API YouTube Transcript

Utilise cette API HTTP pour lire les sous-titres d'une video YouTube (resumer, repondre a une question, citer un passage avec son horodatage), chercher des videos, lister les videos d'une chaine ou d'une playlist, et lire les metadonnees d'une video.

## Auth

Ajoute toujours ce header :

```http
X-API-Key: <API_KEY>
```

## Base URL

Locale :

```text
http://127.0.0.1:8095
```

Via reverse proxy HTTPS (optionnel) :

```text
https://<your-domain>/youtube-api
```

## Regles D'utilisation

- Toutes les reponses sont en JSON.
- Une reponse reussie a toujours `{"ok": true, "data": ...}`.
- Une erreur a toujours `{"ok": false, "error": "...", "error_code": "..."}`.
- Si `ok` vaut `false`, ne devine pas le contenu de la video : rapporte l'erreur.
- Passe toujours la video avec `--data-urlencode 'video=...'`. Une URL non encodee comme `watch?v=X&t=30s` serait coupee en deux parametres.
- `video` accepte l'ID (11 caracteres) ou n'importe quelle URL YouTube : `watch?v=`, `youtu.be/`, `shorts/`, `embed/`, `live/`, `m.youtube.com`, `music.youtube.com`.
- Ajoute `--connect-timeout 5 --max-time 90` : un appel non cache peut prendre plusieurs secondes (proxy + retries).
- Ne pipe pas la reponse vers `python3 -m json.tool` ou `jq` sauf debug humain.
- Les sous-titres auto-generes (`is_generated: true`) n'ont pas de ponctuation fiable et contiennent des erreurs de reconnaissance : garde-le en tete avant de citer mot a mot.
- Pas de traduction cote API : recupere la piste disponible et traduis toi-meme.

## Curl Recommande

```bash
curl -sS --connect-timeout 5 --max-time 90 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8095/transcript' \
  --data-urlencode 'video=https://www.youtube.com/watch?v=aircAruvnKk' \
  --data-urlencode 'lang=fr,en'
```

## Endpoint: Transcript

```http
GET /transcript
```

Parametres :

- `video` : requis. ID ou URL YouTube.
- `lang` : codes de langue par ordre de preference, separes par virgule (`fr,en`). Defaut serveur : `fr,en`. `fr` accepte aussi `fr-FR`, `fr-CA`, etc. Une piste manuelle passe avant une piste auto-generee.
- `strict` : `true` pour echouer si aucune langue demandee n'existe. Par defaut `false` : l'API renvoie la meilleure piste disponible (souvent l'auto-generee dans la langue parlee) avec `fallback: true`.
- `format` : `text` (defaut) ou `segments`.
  - `text` : paragraphes d'environ 45 secondes, chacun prefixe par son horodatage `[m:ss]`. Le plus compact, a privilegier.
  - `segments` : liste brute `[{"t": debut_s, "d": duree_s, "text": "..."}]`. Seulement si tu as besoin d'horodatages precis ligne par ligne.
- `start`, `end` : fenetre en secondes (sous-titres qui commencent dans `[start, end)`). Pour lire un passage precis ou paginer.
- `max_chars` : taille max de la sortie (500 a 200000). Defaut serveur : `20000` (~5k tokens, environ 20 minutes de parole).
- `paragraph_seconds` : duree d'un paragraphe en mode `text`, defaut `45`.
- `include_languages` : `true` pour ajouter `available_languages` (ajoute d'office en cas de `fallback`).

Reponse :

```json
{
  "ok": true,
  "data": {
    "video_id": "aircAruvnKk",
    "url": "https://www.youtube.com/watch?v=aircAruvnKk",
    "title": "But what is a neural network? | Deep learning chapter 1",
    "channel": "3Blue1Brown",
    "language_code": "en",
    "language": "English",
    "is_generated": false,
    "fallback": false,
    "duration": 1120.0,
    "text": "[0:04] This is a 3. It's sloppily written...\n[0:49] ...",
    "chars": 18610,
    "total_chars": 18610,
    "truncated": false,
    "cached": false
  }
}
```

- `title`, `channel` : absents dans de rares cas (sans impact sur les sous-titres).
- `duration` : duree de la video en secondes.
- `fallback: true` : aucune langue demandee n'existe, la piste renvoyee est une autre. Dis-le a l'utilisateur.

### Videos longues : pagination

Si `truncated` vaut `true`, la suite commence a `next_start` (secondes). Rappelle avec `start=<next_start>` en repetant exactement `video`, `lang` et `strict` de la premiere page (sinon la piste choisie peut changer) :

```bash
curl -sS --connect-timeout 5 --max-time 90 \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8095/transcript' \
  --data-urlencode 'video=aircAruvnKk' \
  --data-urlencode 'lang=fr,en' \
  --data-urlencode 'start=1210.536'
```

Passe `next_start` tel quel, sans l'arrondir. Les pages suivantes sont servies depuis le cache (`cached: true`) : pas de cout supplementaire cote YouTube. Ne recupere pas toute une video de 3 heures si la question porte sur un passage : utilise `start`/`end` quand l'utilisateur donne un moment, sinon lis page par page et arrete-toi quand tu as la reponse.

## Endpoint: Languages

```http
GET /languages?video=<id ou URL>
```

Liste des pistes : `[{"code": "en", "name": "English", "generated": false}, ...]`. Rarement utile : `/transcript` choisit deja la piste, et renvoie la liste en cas de fallback ou d'erreur `language_not_found`.

## Endpoint: Search

```http
GET /search?q=<mots-cles>
```

```bash
curl -sS --connect-timeout 5 --max-time 90 \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8095/search' \
  --data-urlencode 'q=recette pain maison' \
  --data-urlencode 'limit=10'
```

Parametres :

- `q` : requis.
- `type` : `video` (defaut), `channel`, `playlist`, ou `all` (resultats mixtes de YouTube).
- `duration` (videos) : `short` (<4 min), `medium` (4-20 min), `long` (>20 min).
- `upload` (videos) : `hour`, `today`, `week`, `month`, `year`. Pour "les plus recentes", filtre avec `upload` : il n'y a pas de tri par date (YouTube l'ignore).
- `sort` : `relevance` (defaut) ou `views`.
- `limit` : nombre d'items, 1 a 100, defaut 20. Demande seulement ce qu'il te faut.
- `next` : voir Pagination.

Reponse :

```json
{"ok": true, "data": {
  "items": [
    {"type": "video", "id": "aircAruvnKk", "title": "But what is a neural network? | Deep learning chapter 1",
     "channel": "3Blue1Brown", "channel_id": "UCYO_jab_esuFRV4b17AJtAw", "duration": "18:40",
     "views": "24 M de vues", "published": "il y a 8 ans", "url": "https://www.youtube.com/watch?v=aircAruvnKk"},
    {"type": "channel", "id": "UC...", "title": "...", "handle": "@...", "subscribers": "220 k abonnés", "description": "...", "url": "..."},
    {"type": "playlist", "id": "PL...", "title": "...", "channel": "...", "video_count": "18 vidéos", "url": "..."}
  ],
  "count": 3,
  "next": "eyJ0Ijo..."
}}
```

- Chaque item a `type`, `id`, `url` ; les autres champs sont omis quand YouTube ne les donne pas (ex. pas de `channel_id` sur une collaboration, pas de `duration` sur un live en cours).
- `views`, `published`, `duration` sont des textes tels qu'affiches par YouTube, en francais (`"2,2 M de vues"`, `"il y a 6 jours"`), approximatifs : pour une date ou un nombre exact, appelle `/video`.
- Un titre peut etre une traduction francaise fournie par YouTube. `/video` et `/transcript` donnent le titre d'origine.

## Endpoint: Channel

```http
GET /channel?channel=<@handle | UC... | URL de chaine>
```

```bash
curl -sS --connect-timeout 5 --max-time 90 \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8095/channel' \
  --data-urlencode 'channel=@3blue1brown' \
  --data-urlencode 'limit=10'
```

Parametres :

- `channel` : requis. `@handle`, ID `UC...`, ou URL (`/@nom`, `/channel/UC...`, `/c/nom`, `/user/nom`).
- `tab` : `videos` (defaut), `shorts`, `streams` (lives passes et a venir), `playlists`.
- `sort` : `latest` (defaut), `popular`, `oldest`. Pas disponible sur `tab=playlists`.
- `limit`, `next` : comme pour search.

Reponse : `data.channel` (`id`, `title`, `handle`, `subscribers`, `video_count`, `description`, `url`, seulement sur la premiere page), puis `items`, `count`, `next`. Les items de `tab=shorts` ont `type: "short"`, sans duree ni date.

## Endpoint: Playlist

```http
GET /playlist?playlist=<PL... ou URL avec list=>
```

Reponse : `data.playlist` (`id`, `title`, `description`, `channel_id`, `url`, premiere page seulement), puis `items`, `count`, `next`. `limit`, `next` comme pour search.

## Endpoint: Video (metadonnees)

```http
GET /video?video=<id ou URL>
```

Reponse : `id`, `title`, `channel`, `channel_id`, `duration_seconds`, `views` (nombre exact), `published` (date ISO exacte), `category`, `keywords`, `description` (complete), `url`, et `is_live` pour un live. Un appel leger (~4 KB) : utilise-le pour une date exacte, la description ou les liens, pas pour le contenu parle (c'est `/transcript`).

## Pagination (search, channel, playlist)

Si `next` n'est pas `null`, il reste des resultats. Rappelle le meme endpoint avec **les memes parametres** et `next=<valeur recue>`, sans la modifier. La suite reprend exactement apres le dernier item recu. `next: null` = fin de liste.

## Erreurs

| `error_code` | HTTP | Que faire |
|---|---|---|
| `invalid_video_id` | 422 | L'entree n'est ni un ID ni une URL YouTube de video. Verifie le lien. |
| `video_unavailable` | 404 | Video supprimee, privee ou mauvais ID. |
| `no_transcripts` | 404 | La video n'a pas de sous-titres. Rien a faire. |
| `language_not_found` | 404 | Avec `strict=true` seulement. Reessaie avec un code de `available_languages`. |
| `age_restricted` | 422 | Video soumise a limite d'age : non accessible. |
| `video_unplayable` | 422 | Raison dans `error` (ex. video membre, region). |
| `blocked` | 502 | YouTube a bloque toutes les IP essayees. Reessaie une fois plus tard, pas en boucle. |
| `po_token_required` | 502 | Piste protegee par YouTube, non recuperable pour l'instant. |
| `network_error`, `upstream_error` | 502 | Probleme reseau ou YouTube. Reessaie une fois. |
| `invalid_reference` | 422 | `channel`, `playlist` ou `next` invalide. Pour `next`, repasse la valeur recue telle quelle. |
| `channel_not_found`, `playlist_not_found` | 404 | Chaine ou playlist inexistante (ou privee). Verifie le handle, ou cherche-la avec `/search?type=channel`. |
| `sort_unavailable` | 422 | Cet onglet n'a pas ce tri. Reessaie avec `sort=latest`. |
| `upstream_rejected` | 502 | YouTube refuse la requete. Ne reessaie pas en boucle, signale-le. |

## Strategie Recommandee

Pour trouver une video : `/search`, puis `/transcript` sur l'`id` choisi. Pour "les dernieres videos de X" : `/channel?channel=@X` (si tu n'as pas le handle, `/search?type=channel&q=X` d'abord). Pour une date de publication exacte : `/video`.

Pour lire une video :

1. Appelle `/transcript` avec la video et `lang` dans la langue de l'utilisateur puis `en` (`lang=fr,en`).
2. Si `truncated` est `false`, tu as tout : reponds.
3. Sinon, pagine avec `next_start` seulement si necessaire.
4. Pour citer, reprends l'horodatage du paragraphe et donne un lien `https://www.youtube.com/watch?v=<id>&t=<secondes>s`.
