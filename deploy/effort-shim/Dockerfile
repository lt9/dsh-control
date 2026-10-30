# 家庭模型链路的 effort 平移层（见 shim.mjs 头注释与 docs/site/MEMORY-P0.md）
# 基座复用 dsh-family 同款镜像，离线可建。
FROM docker.m.daocloud.io/library/node:22-bookworm-slim
COPY shim.mjs /app/shim.mjs
USER node
EXPOSE 8085
CMD ["node", "/app/shim.mjs"]
