FROM node:24.19.0-bookworm-slim@sha256:a9f5f7c91a432850b2a8a7797adf5eadb6c733ceed61167806cee7ea7fbc29df AS node
FROM python:3.12-slim-bookworm@sha256:7753c33391fc9f01d1984375bf375eb6686d52ba10db6043a86634a5ccf90dcf
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home assistant
WORKDIR /opt/life
COPY gateway/package.json gateway/package-lock.json ./gateway/
RUN cd gateway && npm ci --omit=dev
COPY wechat-batching ./wechat-batching
RUN python wechat-batching/build_patch.py gateway/node_modules/@tencent-weixin/openclaw-weixin
COPY pyproject.toml requirements.lock README.md LICENSE ./
COPY server ./server
COPY openclaw-bridge ./openclaw-bridge
COPY proactive-bridge ./proactive-bridge
RUN chmod -R go-w /opt/life/server /opt/life/openclaw-bridge /opt/life/proactive-bridge /opt/life/wechat-batching /opt/life/gateway/node_modules/@tencent-weixin/openclaw-weixin \
    && pip install --no-cache-dir -c requirements.lock . \
    && mkdir /data && chown assistant:assistant /data
ENV LIFE_DATA_DIR=/data LIFE_CONTAINER=1 \
    XDG_CACHE_HOME=/tmp/.cache NPM_CONFIG_CACHE=/tmp/npm-cache \
    LIFE_OPENCLAW_BIN=/opt/life/gateway/node_modules/.bin/openclaw \
    LIFE_BRIDGE_DIR=/opt/life/openclaw-bridge \
    LIFE_WECHAT_PLUGIN_DIR=/opt/life/gateway/node_modules/@tencent-weixin/openclaw-weixin
USER 10001:10001
EXPOSE 18932
ENTRYPOINT ["python", "-m", "life_fleet.runtime"]
