# VENS-DOWNLOADER — Serveur d'extraction yt-dlp

Serveur Flask + yt-dlp qui extrait les URL directes des vidéos TikTok, YouTube,
Facebook, Instagram, Pinterest, Snapchat, X/Twitter. Le site VENS appelle
`/extract` avec l'en-tête `X-API-Key`.

## Déploiement sur Render (recommandé, gratuit)

1. **Génère ta clé API** (obligatoire, secrète) :
   ```bash
   openssl rand -hex 32
   ```
   Copie la valeur — tu vas la coller à 2 endroits.

2. **Pousse ce dossier `ytdlp-server/` sur un repo GitHub** (privé ou public).

3. Va sur https://dashboard.render.com → **New +** → **Web Service** → connecte
   ton repo → sélectionne le dossier `ytdlp-server`.

4. Render détecte le `Dockerfile`. Laisse les valeurs par défaut. Dans
   **Environment**, ajoute :
   - `API_KEY` = la valeur générée à l'étape 1
   - `PORT` = `8000` (déjà par défaut)

5. Clique **Create Web Service**. Attends 3-5 min. Tu obtiens une URL du type
   `https://vens-ytdlp.onrender.com`.

6. Ouvre ton site VENS → **Admin** → **Paramètres** :
   - **URL du serveur yt-dlp** : `https://vens-ytdlp.onrender.com`
   - **Clé API yt-dlp** : la même valeur qu'à l'étape 1
   - **Enregistrer**

Teste avec un lien TikTok — ça doit marcher.

## Variables d'environnement

| Variable  | Requis | Description                                         |
|-----------|--------|-----------------------------------------------------|
| `API_KEY` | oui    | Chaîne aléatoire ≥ 32 caractères. Sans elle, `/extract` refuse tout appel. |
| `PORT`    | non    | Port d'écoute (Render le fournit auto).             |

## Endpoints

- `GET /` — health check, renvoie `{"status":"ok"}`
- `POST /extract` — body JSON `{ "url": "...", "quality": "best|1080|720|480|audio" }`, header `X-API-Key: <ta clé>`

## Sécurité

- Ne commit **jamais** `API_KEY` dans le repo.
- Régénère-la si elle fuit : `openssl rand -hex 32`, mets-la à jour dans Render **et** dans l'admin VENS.
- Le plan Render free s'endort après 15 min d'inactivité (premier appel = 30s de démarrage). Pour rester chaud, passe en plan payant ou ping l'URL toutes les 10 min via un cron externe.

## Test manuel

```bash
curl -X POST https://vens-ytdlp.onrender.com/extract \
  -H "Content-Type: application/json" \
  -H "X-API-Key: TA_CLE_ICI" \
  -d '{"url":"https://www.tiktok.com/@xxx/video/123","quality":"best"}'
```
