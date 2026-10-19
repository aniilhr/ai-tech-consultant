# Single image: three services + web gateway. Exposes one port ($PORT, default 8080).
FROM node:22-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY knowledge_brain/requirements.txt knowledge_brain/requirements.txt
COPY scope_builder/requirements.txt scope_builder/requirements.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r knowledge_brain/requirements.txt -r scope_builder/requirements.txt

COPY inbox_agent/package.json inbox_agent/package-lock.json inbox_agent/
RUN cd inbox_agent && npm ci --silent
COPY web/package.json web/package-lock.json web/
RUN cd web && npm ci --silent

COPY . .
RUN cd inbox_agent && npm run build && npm prune --omit=dev --silent \
 && cd ../web && npm run build && npm prune --omit=dev --silent

ENV VENV=/opt/venv DATA_DIR=/data PORT=8080 NODE_ENV=production
RUN mkdir -p /data
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||8080)+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["bash", "scripts/run-services.sh"]
