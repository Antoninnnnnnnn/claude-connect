# Self-hosted GitHub Proxy

Mini-API FastAPI qui expose les repos GitHub du proprietaire, prives compris, sous les memes URLs que github.com mais sur son propre domaine. Un agent LLM qui n'a pas acces au compte GitHub peut ainsi cloner, lire un fichier, parcourir un repo et pousser des commits, avec la seule cle API commune.

Points importants :

- **Un seul token GitHub** (`GITHUB_TOKEN`) sert toutes les requetes. Quiconque a `API_KEY` a les droits de ce token : utiliser un token fine-grained limite au strict necessaire (voir Configuration).
- **Relais git smart HTTP** : `GET /<owner>/<repo>/info/refs` et `POST /<owner>/<repo>/git-upload-pack|git-receive-pack` sont relayes vers `github.com/<owner>/<repo>.git/...` en streaming dans les deux sens (aucun pack en memoire, compatible `MemoryMax`).
  - Corps bruts (`aiter_raw`) : le `Content-Encoding` gzip passe tel quel, sans decodage/re-encodage.
  - `Git-Protocol` est relaye : protocole v2 de bout en bout.
  - L'en-tete `Authorization` du client n'est jamais transmis ; il est remplace par `x-access-token:<GITHUB_TOKEN>`.
  - Un 401 GitHub n'est jamais renvoye a git (il accuserait la cle API et redemanderait des identifiants) : il devient un 403 ou 404 avec un message `remote:` lisible.
  - Pas de timeout de lecture sur ce relais : GitHub peut mettre longtemps a preparer le pack d'un gros clone.
- **Auth** : `X-API-Key`, `Authorization: Bearer <cle>`, ou Basic avec la cle en mot de passe (ou en nom d'utilisateur). Chaque 401 porte `WWW-Authenticate: Basic` : git n'envoie le `x:<cle>@` de l'URL qu'apres ce defi, et un navigateur affiche sa fenetre de connexion.
- **Navigation** : `/blob`, `/raw`, `/tree`, `/branches`, `/commits`, `/archive` passent par l'API REST GitHub. Les fichiers texte (HTML et SVG compris) sont servis en `text/plain` avec `nosniff` : rien ne s'execute sur ce domaine.
- **Archives** : GitHub redirige vers codeload avec un jeton temporaire dans l'URL ; le proxy suit la redirection lui-meme et streame l'archive, l'URL ne sort pas.
- **Sans token** : les repos publics restent lisibles et clonables (60 appels API/heure), utile pour tester le relais.

## Configuration

Variables dans `../.env` :

- `API_KEY` : cle commune.
- `GH_PORT` : port d'ecoute, defaut `8096`.
- `GITHUB_TOKEN` : token GitHub. Fine-grained conseille (Settings > Developer settings > Personal access tokens > Fine-grained tokens), "All repositories", permissions **Contents : Read and write** et **Metadata : Read-only**. Pour la lecture seule, Contents en Read-only.
- `GH_TIMEOUT` : timeout des appels API (et de connexion du relais git), defaut `30`.
- `GH_MAX_TREE_ENTRIES` : plafond d'entrees de `/tree`, defaut `2000`.

## Lancer

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
set -a && source ../.env && set +a
uvicorn app.main:app --host 127.0.0.1 --port 8096
```

## Tests

```bash
.venv/bin/pytest
```

Les tests unitaires utilisent un faux GitHub (`httpx.MockTransport`). Ils ne valident pas le protocole git lui-meme : apres un changement du relais, faire un vrai `git clone http://x:<API_KEY>@127.0.0.1:8096/octocat/Hello-World` (et `git -c protocol.version=2 ls-remote ...`).

Doc agent : [`../LLM_GITHUB_USAGE.md`](../LLM_GITHUB_USAGE.md).
