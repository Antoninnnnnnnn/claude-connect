# Instructions LLM - Proxy GitHub

Ce service donne acces aux repos GitHub de l'utilisateur, prives compris, avec les memes URLs que github.com : seul le domaine change. Il sert a cloner, lire un fichier, parcourir l'arborescence, lire l'historique et pousser des commits.

Quand l'utilisateur partage un lien `https://github.com/<owner>/<repo>/...`, remplace `https://github.com` par la base URL ci-dessous. Le reste du chemin est identique.

## Auth

Deux formes acceptees, avec la meme cle :

```http
X-API-Key: <API_KEY>
```

ou HTTP Basic, cle en mot de passe (nom d'utilisateur quelconque) : `https://x:<API_KEY>@<host>/...`. C'est la forme a utiliser avec `git`.

## Base URL

Locale :

```text
http://127.0.0.1:8096
```

Via HTTPS (Cloudflare Tunnel) :

```text
https://<github-host>
```

## Regles D'utilisation

- Pour travailler sur le code (lire beaucoup de fichiers, lancer des tests, modifier), **clone**. Pour lire un ou deux fichiers, utilise `/blob`.
- Les endpoints JSON repondent `{"ok": true, "data": ...}` ou `{"ok": false, "error": "...", "error_code": "..."}`. `/blob`, `/raw` et `/archive` renvoient le fichier brut.
- `not_found` (404) veut dire : le repo ou le fichier n'existe pas, **ou** le token n'y a pas acces. GitHub ne distingue pas les deux.
- Ne pousse que si l'utilisateur l'a demande. Prefere une nouvelle branche a un push direct sur la branche par defaut, sauf demande explicite.
- Ne force-push jamais sans demande explicite.
- La cle API reste dans l'URL du remote : ne l'affiche pas dans tes reponses (par exemple en citant `git remote -v` ou `.git/config`).

## Cloner

```bash
git clone https://x:<API_KEY>@<github-host>/<owner>/<repo>.git
```

Le `.git` final est optionnel. Le protocole git est relaye tel quel vers github.com : `fetch`, `pull`, `ls-remote`, `--depth 1`, `--branch` fonctionnent comme d'habitude.

Pour un gros repo dont tu n'as besoin que du dernier etat :

```bash
git clone --depth 1 https://x:<API_KEY>@<github-host>/<owner>/<repo>.git
```

## Pousser

Le remote `origin` du clone pointe deja vers le proxy :

```bash
git checkout -b claude/ma-modif
git commit -am "..."
git push -u origin claude/ma-modif
```

- Une requete est limitee a 100 Mo par Cloudflare : un push plus gros echoue. Pousse en plusieurs fois.
- `403` au push : le token n'a pas le droit d'ecrire sur ce repo. Rapporte-le, ne reessaie pas.
- Pour ouvrir une pull request, donne a l'utilisateur le lien `https://github.com/<owner>/<repo>/compare/<branche>` : le proxy ne cree pas de PR.

## Curl Recommande

```bash
curl -sS --connect-timeout 5 --max-time 60 \
  -H 'X-API-Key: <API_KEY>' \
  'https://<github-host>/<owner>/<repo>/blob/main/README.md'
```

## Endpoints

### Lister les repos de l'utilisateur

```http
GET /repos?limit=50&page=1&sort=pushed
```

- `sort` : `pushed` (defaut), `updated`, `created`, `full_name`.
- `has_more: true` : demande `page+1`.

```json
{"ok": true, "data": {"page": 1, "count": 2, "has_more": false, "repos": [
  {"full_name": "moi/projet", "private": true, "description": "...", "default_branch": "main",
   "language": "Python", "fork": false, "archived": false, "size_kb": 812,
   "pushed_at": "2026-09-30T18:02:11Z", "clone_url": "https://<github-host>/moi/projet.git", "can_push": true}
]}}
```

### Infos d'un repo

```http
GET /<owner>/<repo>
```

Meme forme qu'une entree de `/repos`. Donne la branche par defaut et l'URL de clone.

### Lire un fichier

```http
GET /<owner>/<repo>/blob/<ref>/<chemin>
GET /<owner>/<repo>/raw/<ref>/<chemin>
```

- `<ref>` : branche, tag ou SHA. Les deux routes sont identiques et renvoient le contenu brut.
- Branche avec un `/` dans le nom (`feature/x`) : passe-la en parametre, `GET /<owner>/<repo>/blob/<chemin>?ref=feature/x`.
- Les fichiers texte (HTML et SVG compris) sont servis en `text/plain` ; images et PDF avec leur type.

### Parcourir l'arborescence

```http
GET /<owner>/<repo>/tree
GET /<owner>/<repo>/tree/<ref>
GET /<owner>/<repo>/tree/<ref>/<dossier>
```

- Sans `<ref>` : branche par defaut.
- `recursive=true` : tous les fichiers sous le dossier, pas seulement les enfants directs. Ideal pour avoir la carte d'un repo en un appel.
- `ref=feature/x` : meme regle que pour `/blob`.
- Plafonne a 2000 entrees ; `truncated: true` si la liste est coupee. Demande alors un sous-dossier.

```json
{"ok": true, "data": {"ref": "main", "path": "src", "count": 2, "entries": [
  {"name": "lib", "type": "dir", "size": 0},
  {"name": "app.py", "type": "file", "size": 1834}
]}}
```

En mode `recursive`, chaque entree a `path` (chemin complet) au lieu de `name`.

### Branches

```http
GET /<owner>/<repo>/branches
```

### Historique

```http
GET /<owner>/<repo>/commits?ref=main&path=src/app.py&limit=20&page=1
```

- `ref` : branche, tag ou SHA (defaut : branche par defaut).
- `path` : seulement les commits qui touchent ce chemin.

### Archive

```http
GET /<owner>/<repo>/archive/<ref>.tar.gz
GET /<owner>/<repo>/archive/<ref>.zip
```

Snapshot du repo sans historique, utile si `git` n'est pas disponible.

## Erreurs

| `error_code` | Sens |
|---|---|
| `unauthorized` (401) | Cle API absente ou fausse. |
| `not_found` (404) | N'existe pas, ou le token n'y a pas acces. |
| `forbidden` (403) | Le token n'a pas la permission (ecriture, par exemple). |
| `rate_limited` (429) | Quota GitHub atteint, reessaie plus tard. |
| `empty_repository` (409) | Repo sans aucun commit. |
| `bad_token` (502) | Le token GitHub du serveur est expire ou revoque : previens l'utilisateur. |
| `no_token` (503) | Le serveur n'a pas de token GitHub : seuls les repos publics sont lisibles. |
| `invalid_request` (422) | Nom de repo, chemin ou ref invalide. |

Cote `git`, les refus arrivent en `remote: ...` suivi d'un 403 ou 404.
