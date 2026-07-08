# Docker / Portainer Deployment

Der Server kommuniziert per **stdio** und kann daher nicht direkt als
Netzwerkdienst laufen. Das mitgelieferte Docker-Setup nutzt `mcp-proxy`,
um `server.py` als Kindprozess zu starten und zusaetzlich ueber SSE/HTTP
auf Port `8096` bereitzustellen - dadurch ist der Server als
dauerhaft laufender Portainer-Stack nutzbar.

## Enthaltene Dateien

- `Dockerfile` - baut das Image aus `server.py` und `requirements.txt`,
  installiert Abhaengigkeiten inkl. `mcp-proxy`.
- `docker-compose.yml` - Stack-Definition fuer Portainer/Docker Compose.
- `.env.example` - Vorlage fuer die benoetigten Umgebungsvariablen.
- `.dockerignore` - schliesst unnoetige Dateien vom Build aus.

## Einrichtung in Portainer

1. In Portainer unter Stacks -> Add stack ein neues Stack anlegen
   (z. B. per Git-Repository-Verweis auf dieses Repo).
2. Die Umgebungsvariablen aus `.env.example` setzen, insbesondere:
   - `ITOP_URL` - Basis-URL der iTop-Instanz
   - `ITOP_TOKEN` - Auth-Token (empfohlen) oder `ITOP_USER` + `ITOP_PASSWORD`
   - `ITOP_VERSION`, `ITOP_VERIFY_SSL`, `ITOP_TIMEOUT` optional anpassen
3. Stack starten. Portainer baut das Image automatisch anhand des Dockerfiles.
4. Nach dem Start ist der MCP-Server erreichbar unter:
   `http://<Docker-Host>:8096/sse`

## Lokal mit Docker Compose

```bash
cp .env.example .env
# .env mit echten Werten befuellen
docker compose up -d --build
```

## Hinweis zu Zugangsdaten

Die Zugangsdaten (Token oder Benutzername/Passwort) werden ausschliesslich
als Umgebungsvariablen im Container gesetzt und nicht im Image gespeichert.
In Portainer sollten sie ueber die integrierte Umgebungsvariablen-Verwaltung
gepflegt werden, nicht im Klartext im Stack-File.
