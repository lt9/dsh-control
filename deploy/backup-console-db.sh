#!/usr/bin/env bash
# dsh-control 的库备份：只 dump 我们自己的 dsh_control 库。
#
# 为什么要它：这台控制台的库（instances / tokens / jobs / credentials）里存着
# 每台实例的登录密钥与全部开通档案。容器重建、误删库都会让这些一起没了，
# 而他的 21 个站点跟这个库无关——所以这里只碰 dsh_control 一个库，
# 别的库一律不看、不导出、不写。
set -euo pipefail

PG_CONTAINER="${PG_CONTAINER:-tierai-prod-postgres-1}"
DB_NAME="${DB_NAME:-dsh_control}"
OUT_DIR="${OUT_DIR:-$HOME/backups/dsh-control}"
KEEP="${KEEP:-14}"                      # 保留最近 14 份
TS="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$OUT_DIR"
STAMP="$(date '+%F %T')"

# -Fc 是自定义压缩格式，恢复用 pg_restore，比纯文本更适合按表挑着恢复。
# 角色用容器自带的默认超级用户角色（容器环境里就有，不需要把密码抄进脚本）。
if ! docker exec "$PG_CONTAINER" sh -c \
      'pg_dump -U "${POSTGRES_USER:-postgres}" -d "$1" -Fc' sh "$DB_NAME" \
      > "$OUT_DIR/$DB_NAME-$TS.dump"; then
  echo "[$STAMP] 备份失败：pg_dump 没跑成（容器 $PG_CONTAINER 不在？库 $DB_NAME 不在？）" >&2
  rm -f "$OUT_DIR/$DB_NAME-$TS.dump"
  exit 1
fi

# 备份写完必须验一眼能不能读，否则"有备份"是假的
if ! docker exec -i "$PG_CONTAINER" pg_restore --list < "$OUT_DIR/$DB_NAME-$TS.dump" > /dev/null 2>&1; then
  echo "[$STAMP] 备份可疑：归档读不出目录（$OUT_DIR/$DB_NAME-$TS.dump）" >&2
  exit 1
fi

SIZE="$(du -h "$OUT_DIR/$DB_NAME-$TS.dump" | cut -f1)"
# 只清这个目录里我们自己的文件，别的路径一律不碰
ls -1t "$OUT_DIR/$DB_NAME-"*.dump 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
  rm -f "$old"
done
echo "[$STAMP] 好了：$OUT_DIR/$DB_NAME-$TS.dump（$SIZE，保留最近 $KEEP 份）"
