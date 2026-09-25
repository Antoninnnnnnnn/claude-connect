# Instructions LLM - API YouTube Transcript

Utilise cette API HTTP pour lire les sous-titres d'une video YouTube : resumer, repondre a une question sur la video, citer un passage avec son horodatage.

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

## Strategie Recommandee

1. Appelle `/transcript` avec la video et `lang` dans la langue de l'utilisateur puis `en` (`lang=fr,en`).
2. Si `truncated` est `false`, tu as tout : reponds.
3. Sinon, pagine avec `next_start` seulement si necessaire.
4. Pour citer, reprends l'horodatage du paragraphe et donne un lien `https://www.youtube.com/watch?v=<id>&t=<secondes>s`.
