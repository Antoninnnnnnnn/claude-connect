# Instructions LLM - API École Directe

Utilise cette API HTTP locale pour accéder au compte École Directe de l'utilisateur. Cela te permet de lire son emploi du temps, ses devoirs, ses notes et ses messages de façon sécurisée sans manipuler ses identifiants.

## Auth

Ajoute toujours ce header pour t'authentifier :

```http
X-API-Key: <API_KEY>
```

## Base URL

Locale :

```text
http://127.0.0.1:8093
```

Via reverse proxy HTTPS (optionnel) :

```text
https://<your-domain>/ecoledirecte-api
```

## Règles D'utilisation

- Toutes les réponses sont en JSON, compactées et épurées pour économiser le contexte.
- Une réponse réussie a toujours `{"ok": true, "data": ...}`.
- Une erreur a toujours `{"ok": false, "error": "..."}`, avec un code HTTP d'erreur.
- Si l'API retourne une erreur `MFA_REQUIRED` (code HTTP 401), cela signifie que la session École Directe demande à valider un QCM. Tu dois récupérer la question dans la réponse, interroger l'utilisateur si tu n'as pas la réponse, et la soumettre via `POST /mfa`.
- Un code HTTP 503 signale un login en cours ou un backoff après un échec : le message donne le délai. Attends ce délai, ne réessaie pas en boucle. École Directe bloque un compte après quelques logins refusés, donc les tentatives sont volontairement espacées.
- Un code HTTP 502 sur `credentials rejected` signifie que les identifiants ou les réponses QCM sont à corriger : ne réessaie pas, signale-le à l'utilisateur.
- Utilise `curl -sS -G --data-urlencode` pour les paramètres d'URL.
- Ajoute `--connect-timeout 5 --max-time 60` pour éviter de rester bloqué (l'API de l'éducation nationale peut parfois être lente).
- Ne pipe pas la réponse vers `python3 -m json.tool` ou `jq`, lis le JSON compact.

## Endpoint: Schedule (Emploi du temps)

Récupère les cours et les événements de l'emploi du temps.

```http
GET /schedule
```

Paramètres :
```text
start_date   (optionnel) Date de début au format YYYY-MM-DD (par défaut : aujourd'hui).
end_date     (optionnel) Date de fin au format YYYY-MM-DD (par défaut : aujourd'hui + 7 jours).
```

Exemple recommandé :
```bash
curl -sS --connect-timeout 5 --max-time 60 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  -G 'http://127.0.0.1:8093/schedule' \
  --data-urlencode 'start_date=2026-06-03' \
  --data-urlencode 'end_date=2026-06-05'
```

Champs de retour :
Renvoie un tableau de cours avec `start`, `end`, `subject`, `prof`, `room`, `is_cancelled` (annulé) et `is_modified` (modifié).


## Endpoint: Homework (Devoirs)

Récupère les devoirs à faire, triés par date.

```http
GET /homework
```

Exemple recommandé :
```bash
curl -sS --connect-timeout 5 --max-time 60 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  'http://127.0.0.1:8093/homework'
```

Champs de retour :
Retourne un dictionnaire groupé par date `YYYY-MM-DD`.
Chaque élément contient : `subject`, `done` (true/false) et `content` (le texte du devoir décodé en clair).


## Endpoint: Grades (Notes)

Récupère les moyennes trimestrielles et la liste des notes.

```http
GET /grades
```

Paramètres :
```text
annee_scolaire   (optionnel) Année scolaire au format YYYY-YYYY (ex: "2025-2026"). Par défaut : l'année en cours.
```

Exemple recommandé :
```bash
curl -sS --connect-timeout 5 --max-time 60 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  'http://127.0.0.1:8093/grades'
```

Champs de retour :
Un objet contenant :
- `periodes` : Les trimestres avec leurs identifiants et les moyennes (élève / classe).
- `notes` : La liste chronologique des évaluations avec la date, le sujet (subject), le nom du devoir (name), la note (grade), sur combien (out_of), le coefficient (coef) et la moyenne de classe (class_avg).


## Endpoint: Messages

Récupère les messages envoyés par les professeurs et l'administration sur la messagerie interne.

```http
GET /messages
```

Paramètres :
```text
annee_scolaire   (optionnel) Format YYYY-YYYY.
```

Exemple recommandé :
```bash
curl -sS --connect-timeout 5 --max-time 60 \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  'http://127.0.0.1:8093/messages'
```

Champs de retour :
Une liste de messages avec `date`, `subject`, `from` (expéditeur), `read` (true/false) et `has_files`.


## Endpoint: MFA (Gestion du QCM)

Si l'API te renvoie une erreur 401 avec `{"error": "MFA_REQUIRED"}`, c'est qu'École Directe demande à valider un nouvel appareil avec un QCM de sécurité.

**Flux à suivre :**
1. Lis la réponse de l'erreur 401 qui contient `{"mfa_data": {"question": "...", "propositions": ["...", "..."]}}`.
2. Trouve la réponse parmi les `propositions` (demande à l'utilisateur si tu ne sais pas).
3. Soumets la réponse via POST :

```bash
curl -sS -X POST \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: <API_KEY>' \
  -d '{"answer": "LA REPONSE EXACTE CHOISIE PARMI LES PROPOSITIONS"}' \
  'http://127.0.0.1:8093/mfa'
```

4. L'API renverra `{"ok": true, "message": "MFA answer submitted, login resuming"}`.
5. Tu peux alors relancer ta requête originelle (`/schedule`, `/homework`, etc.). L'API relancera le login et te fournira tes données. S'il y a une deuxième question, recommence le processus.


## Endpoint: Status (diagnostic de session)

À utiliser quand une requête échoue, pour savoir pourquoi sans relancer de login.

```
GET /status
```

```bash
curl -sS \
  -H 'X-API-Key: <API_KEY>' \
  'http://127.0.0.1:8093/status'
```

Renvoie l'état de connexion, le nombre d'échecs consécutifs, le délai de backoff restant (`retry_in_s`) et la dernière erreur. `GET /health` reste public mais ne donne que `{"status": "up"}`.
