#!/usr/bin/env bash
# pmc.sh —— 打一条带会话标记的评论（一条命令，前缀与台账由评论实现负责）
#
# 用法：
#   bash super_agent/scripts/pmc.sh --project <短名> --issue <n> <文本文件> [会话键名] [--dry-run]
#
# 例：
#   bash super_agent/scripts/pmc.sh --project demo --issue 32 body.txt pm-demo-1a2b3c4d
#   bash super_agent/scripts/pmc.sh --project demo --issue 32 body.txt pm-demo-1a2b3c4d --dry-run
#
# 目标项目**必须**由参数给：本脚本属于本仓，不能把本仓当成目标项目。
# 会话键名可由第三个位置参数或 PM_SESSION_KEY 给；缺了不发（静默发一条不叫铃的
# verdict 比不发更糟）。
#
# 退出码：0 成功；1 参数/文件问题或发送失败。
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAFKC_ROOT="$(cd "$SELF_DIR/../.." && pwd)"

die() { echo "pmc: $*" >&2; exit 1; }

usage() {
  echo "用法: bash super_agent/scripts/pmc.sh --project <短名> --issue <n> <文本文件> [会话键名] [--dry-run]" >&2
  exit 1
}

DRY=0
PROJECT=""
ISSUE=""
ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --project) PROJECT="${2:-}"; shift 2 ;;
    --project=*) PROJECT="${1#*=}"; shift ;;
    --issue) ISSUE="${2:-}"; shift 2 ;;
    --issue=*) ISSUE="${1#*=}"; shift ;;
    -h|--help) usage ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

[ -n "$PROJECT" ] || die "缺 --project <短名>（本脚本不猜目标项目）"

# 位置参数：正文文件，可选会话键名。issue 可由 --issue 或首位置给。
if [ -z "$ISSUE" ] && [ "${#ARGS[@]}" -ge 2 ]; then
  ISSUE="${ARGS[0]}"
  ARGS=("${ARGS[@]:1}")
fi
BODY_FILE="${ARGS[0]:-}"
ARG_KEY="${ARGS[1]:-}"

[ -n "$ISSUE" ] || usage
[ -n "$BODY_FILE" ] || usage

case "$ISSUE" in
  ''|*[!0-9]*) die "issue 号必须是数字，收到：$ISSUE" ;;
esac

if [ ! -e "$BODY_FILE" ]; then
  die "评论正文文件不存在：$BODY_FILE"
fi
if [ ! -f "$BODY_FILE" ]; then
  die "评论正文路径不是普通文件：$BODY_FILE"
fi
if [ ! -s "$BODY_FILE" ]; then
  die "评论正文文件是空的：$BODY_FILE"
fi

BODY="$(cat "$BODY_FILE")"
if [ -z "${BODY//[[:space:]]/}" ]; then
  die "评论正文只有空白字符：$BODY_FILE"
fi

# 会话标记（#87 遗留）：引擎靠正文末行 `<!-- session: <键名> -->` 归属裁决，
# 不写就判 unverified ⇒ ready **不叫 release 铃**。历史实证：#76／#78／#79／#91
# 的 verdict 全部缺此标记，铃一次没响过。键名由会话自报（只有它自己知道自己是
# 哪把钥匙），可用 PM_SESSION_KEY 或第三个位置参数给；缺了就停。
SESSION_KEY="${PM_SESSION_KEY:-$ARG_KEY}"
if [ -z "$SESSION_KEY" ]; then
  cat >&2 <<'EOF'
pmc: 缺会话键名。

  bash super_agent/scripts/pmc.sh --project <短名> --issue <n> <正文文件> <你的键名>
  # 或 export PM_SESSION_KEY=<你的键名>

键名在会话状态台账里；只有你自己的会话知道自己是哪把。不给也能发，
但引擎会把本条判 unverified，ready 因此**不叫 release 铃**。
EOF
  exit 1
fi

ISSUE_CLI=(-m super_agent.issue_cli)

if [ "$DRY" -eq 1 ]; then
  echo "[pmc] dry-run，不发送。将执行："
  echo "  cd $SAFKC_ROOT && python ${ISSUE_CLI[*]} comment --role pm --project $PROJECT \\"
  echo "      --issue $ISSUE --session-key $SESSION_KEY --body <<< <正文>"
  echo "[pmc] 正文前 80 字："
  printf '  %s\n' "$(printf '%s' "$BODY" | head -c 80)"
  exit 0
fi

cd "$SAFKC_ROOT" || die "进不去本仓根目录：$SAFKC_ROOT"

if ! OUT="$(python "${ISSUE_CLI[@]}" comment --role pm --project "$PROJECT" \
      --issue "$ISSUE" --session-key "$SESSION_KEY" --body "$BODY" 2>&1)"; then
  echo "$OUT" >&2
  die "评论发送失败（issue #$ISSUE），上面是原始报错"
fi

echo "pmc: commented #$ISSUE ok"
printf '%s\n' "$OUT" | sed 's/^/  /'
