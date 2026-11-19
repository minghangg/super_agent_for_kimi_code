#!/usr/bin/env bash
# pmd.sh —— 派单一条龙：派会话 → 绑会话 → 打 stage label → 发带标记的评论
#
# 用法：
#   bash super_agent/scripts/pmd.sh --project <短名> --cwd <目标项目路径> #        <issue号> <worker|qa> <任务书文件> [slug] [--dry-run] [--brief <短令>] [--full-task]
#
# 例：
#   bash super_agent/scripts/pmd.sh --project demo --cwd /path/to/target 32 worker /tmp/task.txt
#   bash super_agent/scripts/pmd.sh --project demo --cwd /path/to/target 32 qa /tmp/review.txt
#
# 目标项目**必须**由 --project／--cwd 给：本脚本属于本仓，不能把本仓当成目标项目
# （把 --cwd 默认成 safkc 会让派出的会话在错误的仓里干活）。
# 章程根取 agent.json 的 `charter.root`，未配置即 die —— 不许起无章程会话。
#
# 红线：绝不给 --session 场景带 --provider/--model/--effort；绝不 push；
#       脚本内不写任何密钥。
# 退出码：0 成功；1 参数/前置检查失败；后续步骤失败时打印已完成步骤。
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAFKC_ROOT="$(cd "$SELF_DIR/../.." && pwd)"

die() { echo "[pmd] 失败：$*" >&2; exit 1; }
step() { echo "[pmd] $*"; }

# __SELFCHECK_BEGIN__（issue #13：自检抽成函数，便于单测；标记段之间可单独 source）
# 判据三条，任一不过即拒发：
#   1) 任务书首行去空白后必须非空——首行为空时 `grep -F ""` 恒真，整段自检空转；
#   2) 评论正文命中任务书「非空行」≥ min(6, 非空行总数)——防只塞首行冒充全文；
#   3) 评论文件字节数 ≥ 任务书字节数 × 90%——防正文被截断。
# 评论**始终全文留档**（用户 2026-11-17 定）：issue 评论是唯一永久留档，
# 所以任务书全文必须内联进评论（折叠段），不能只给路径——路径指向的本地文件
# 会随回收站清理消失。
# 注意：**精简的是发给 worker 的消息（--task），不是评论**。两者不同：
#   评论 = 全文（永久留档）    --task = 短令 + 指向（省被派方上下文）
pmd_build_comment() {
  local task="$1" out="$2" summary="${3:-}"
  {
    printf '%s\n\n' "$summary"
    printf '%s\n' '---'
    printf '%s\n' '<details><summary>任务书全文</summary>'
    printf '\n```\n'
    cat "$task"
    printf '\n```\n'
    printf '%s\n' '</details>'
  } > "$out"
}


pmd_selfcheck_comment() {
  local task="$1" cmt="$2"
  local head_line nonempty need=0 hit=0 line bytes task_bytes
  head_line="$(head -1 "$task" | tr -d '[:space:]')"
  if [ -z "$head_line" ]; then
    echo "任务书首行为空，拒绝发送" >&2
    return 1
  fi
  nonempty="$(grep -c '[^[:space:]]' "$task" 2>/dev/null || true)"
  nonempty="${nonempty:-0}"
  if [ "$nonempty" -le 0 ]; then
    echo "任务书无任何非空行，拒绝发送" >&2
    return 1
  fi
  need=$(( nonempty < 6 ? nonempty : 6 ))
  while IFS= read -r line || [ -n "$line" ]; do
    if grep -qF -- "$line" "$cmt" 2>/dev/null; then
      hit=$(( hit + 1 ))
      [ "$hit" -ge "$need" ] && break
    fi
  done < <(grep '[^[:space:]]' "$task")
  if [ "$hit" -lt "$need" ]; then
    echo "评论正文只命中任务书非空行 ${hit}/${need} 条，疑似截断，拒绝发送" >&2
    return 1
  fi
  bytes="$(wc -c < "$cmt" | tr -d ' ')"
  task_bytes="$(wc -c < "$task" | tr -d ' ')"
  if [ $(( bytes * 10 )) -lt $(( task_bytes * 9 )) ]; then
    echo "评论 ${bytes} 字节 < 任务书 ${task_bytes} 字节的 90%，疑似截断，拒绝发送" >&2
    return 1
  fi
  echo "自检通过：非空行命中 ${hit}/${need}，评论 ${bytes} 字节 / 任务书 ${task_bytes} 字节" >&2
  return 0
}
# __SELFCHECK_END__

usage() {
  cat >&2 <<'EOF'
用法: bash super_agent/scripts/pmd.sh --project <短名> --cwd <目标项目路径>         <issue号> <worker|qa> <任务书文件> [slug] [--dry-run] [--brief <短令>] [--full-task]

  --project      目标项目短名（必填；不猜）
  --cwd          目标项目工作目录（必填；不默认成本仓）
  issue号        数字，如 32
  worker|qa      worker 走 rotation 轮换表；qa 走 --profile qa（config 画像，回退内置默认）
  任务书文件     非空文本文件
  slug           可选，用于会话名；省略则取任务书文件名去扩展
EOF
  exit 1
}

DRY=0
PM_KEY="${PM_SESSION_KEY:-}"
PROJECT=""
TARGET_CWD=""
ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --full-task) FULL_TASK=1; shift ;;
    --brief)   TASK_BRIEF="${2:-}"; shift 2 ;;
    --pm-key)  PM_KEY="${2:-}"; shift 2 ;;
    --project) PROJECT="${2:-}"; shift 2 ;;
    --project=*) PROJECT="${1#*=}"; shift ;;
    --cwd)     TARGET_CWD="${2:-}"; shift 2 ;;
    --cwd=*)   TARGET_CWD="${1#*=}"; shift ;;
    -h|--help) usage ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

[ -n "$PROJECT" ] || die "缺 --project <短名>（本脚本不猜目标项目）"
[ -n "$TARGET_CWD" ] || die "缺 --cwd <目标项目路径>（本脚本不把目标项目默认成本仓）"

[ "${#ARGS[@]}" -eq 3 ] || [ "${#ARGS[@]}" -eq 4 ] || usage

ISSUE="${ARGS[0]}"
ROLE="${ARGS[1]}"
TASK_FILE="${ARGS[2]}"
SLUG="${ARGS[3]:-}"

case "$ISSUE" in
  ''|*[!0-9]*) die "issue 号必须是数字，收到：$ISSUE" ;;
esac
case "$ROLE" in
  worker|qa) ;;
  *) die "第二个参数必须是 worker 或 qa，收到：$ROLE" ;;
esac

# ---------- 第 1 步：前置检查 ----------
[ -e "$TASK_FILE" ] || die "任务书不存在：$TASK_FILE"
[ -f "$TASK_FILE" ] || die "任务书不是文件：$TASK_FILE"
[ -s "$TASK_FILE" ] || die "任务书是空的：$TASK_FILE"
# issue #13：首行为空会让后面的全文自检整体空转（grep -F "" 恒真），提前拦死
if [ -z "$(head -1 "$TASK_FILE" | tr -d '[:space:]')" ]; then
  die "任务书首行为空，拒绝发送：$TASK_FILE"
fi

# issue #12：pmd 派的是**新会话**（一次性 worker / 新 QA），门禁只对「复用已有会话」
# 有意义。这里显式说明并跳过——不要在 pmd 里假装做了门禁检查。
# （复用会话的入口是 `dispatch.py --session <sid>`，那边已经内建门禁。）
if [ -n "${CTX_GATE_SESSION:-}" ]; then
  step "ctx 门禁（复用会话 $CTX_GATE_SESSION）"
  if ! ( cd "$SAFKC_ROOT" && python -m super_agent.ctx_gate --id "$CTX_GATE_SESSION" --role pm ${CTX_FORCE:+--force --operator pm} ); then
    die "ctx 门禁拒绝派单（见上方 [ctx-gate] 输出）"
  fi
fi

WARN=()
grep -q '验收' "$TASK_FILE" 2>/dev/null || WARN+=("任务书缺「验收」节")
grep -q '红线' "$TASK_FILE" 2>/dev/null || WARN+=("任务书缺「红线」节")
for w in "${WARN[@]:-}"; do
  [ -n "$w" ] && echo "[pmd] 警告：$w（不拦，继续）" >&2
done

if [ -z "$SLUG" ]; then
  SLUG="$(basename "$TASK_FILE")"
  SLUG="${SLUG%.*}"
fi
DATE="$(date +%Y%m%d)"

# ---------- 第 2 步：组装 dispatch 命令 ----------
if [ "$ROLE" = "worker" ]; then
  CWD="$TARGET_CWD"
  NAME="wk-ISS-${ISSUE}-${SLUG}-${DATE}"
  if [ ! -d "$CWD" ]; then
    die "worker 的 cwd 不存在：$CWD"
  fi
  MODEL_ARGS_RAW="$( cd "$SAFKC_ROOT" && python -m super_agent.model_rotation 2>/dev/null )"
  if [ -z "$MODEL_ARGS_RAW" ]; then
    die "model_rotation 没有输出（检查 python -m super_agent.model_rotation）"
  fi
  # 拆成数组：--provider X --model Y --effort Z
  read -r -a MODEL_ARGS <<< "$MODEL_ARGS_RAW"
  STAGE_LABEL="stage:dev"
  BIND_ROLE="worker"
else
  CWD="$TARGET_CWD"
  NAME="qa-${PROJECT}-ISS-${ISSUE}"
  MODEL_ARGS_RAW="$( ( cd "$SAFKC_ROOT" && python -m super_agent.model_rotation --profile qa --json ) 2>/dev/null \
    | python -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
if d.get('source') != 'config':
    raise SystemExit(1)
print('--provider codebuddy --model %s --effort %s' % (d['model'], d['effort']))
" 2>/dev/null)"
  if [ -z "$MODEL_ARGS_RAW" ]; then
    echo "[pmd] ⚠ 取不到 qa 画像（缺配置/坏配置/无此角色），回退内置默认" >&2
    MODEL_ARGS_RAW="--provider codebuddy --model cb-deepseek-v4.1-flash --effort high"
  fi
  read -r -a MODEL_ARGS <<< "$MODEL_ARGS_RAW"
  STAGE_LABEL="stage:qa"
  BIND_ROLE="qa"
fi

if [ ! -d "$CWD" ]; then
  die "cwd 不存在：$CWD"
fi

# 章程注入（用户 2026-11-16 令）：worker／QA 会话必须带各自章程，
# 否则「默认测试级别」等规则读不到（wk-ISS-87-impl 实例：无章程 ⇒ 自跑三轮全量）。
# 本仓没有 `assistants/`，章程根由 agent.json 的 `charter.root` 给；未配置即 die，
# 不许静默起一个没有章程的会话。dry-run 只报告解析结果，不拦——它不建会话。
CHARTER_ROOT="$( cd "$SAFKC_ROOT" && python -c "from super_agent import agent_config as c; print(c.charter_root())" 2>/dev/null )"
if [ "$BIND_ROLE" = "worker" ]; then
  PERSONA_FILE="${CHARTER_ROOT:+$CHARTER_ROOT/assistants/worker.md}"
else
  PERSONA_FILE="${CHARTER_ROOT:+$CHARTER_ROOT/assistants/qa.md}"
fi
if [ -z "$CHARTER_ROOT" ]; then
  [ "$DRY" -eq 1 ] || die "charter.root 未配置，不许起无章程会话（在 agent.json 里给 charter.root）"
  PERSONA_FILE=""
  PERSONA_NOTE="(charter.root 未配置)"
elif [ ! -f "$PERSONA_FILE" ]; then
  [ "$DRY" -eq 1 ] || die "章程文件不存在：$PERSONA_FILE（不许起无章程的 $BIND_ROLE）"
  PERSONA_NOTE="$PERSONA_FILE 不存在"
else
  PERSONA_NOTE="$PERSONA_FILE"
fi

# 2026-11-17（用户令）：派给 wk／QA 的 --task 只发短令，不再塞整份任务书。
# 理由：任务书全文已在 issue（或本地文件）里，再内联一遍＝两个真源；且长任务书
# 让被派方一上来就吃掉大量上下文。这与 supervisor 派 PM 的短令同形。
# --full-task 保留旧行为（整份塞进 --task），供离线场景。
TASK_BYTES="$(wc -c < "$TASK_FILE" | tr -d ' ')"
if [ -z "${TASK_BRIEF:-}" ]; then
  TASK_BRIEF="$(head -1 "$TASK_FILE" | tr -d '')"
fi
if [ "${FULL_TASK:-0}" = "1" ]; then
  TASK_TEXT="$(cat "$TASK_FILE")"
else
  TASK_TEXT="$(printf '%s\n\n任务书全文：%s（%s 字节，必须先读它再动手）' \
    "$TASK_BRIEF" "$TASK_FILE" "$TASK_BYTES")"
  if [ -n "${TASK_POINTER:-}" ]; then
    TASK_TEXT="$(printf '%s\n%s' "$TASK_TEXT" "$TASK_POINTER")"
  fi
fi

PERSONA_TEXT=""
if [ -n "$PERSONA_FILE" ] && [ -f "$PERSONA_FILE" ]; then
  PERSONA_TEXT="$(cat "$PERSONA_FILE")"
fi

DISPATCH_CMD=(python -m super_agent.dispatch_cli
  "${MODEL_ARGS[@]}"
  --cwd "$CWD"
  --name "$NAME"
  --persona "$PERSONA_TEXT"
  --task "$TASK_TEXT"
  --permission-mode auto
  --detach)

LABEL_GREP="$(grep -oE 'L[0-3]' "$TASK_FILE" 2>/dev/null | head -1)"
LEVEL="${LABEL_GREP:-未标注}"

COMMENT_SUMMARY="派单 ${NAME}（session <待解析>）｜级别 ${LEVEL}｜收口写本单评论"
# ---------- dry-run ----------
if [ "$DRY" -eq 1 ]; then
  echo "[pmd] dry-run，不执行。将对 issue #$ISSUE 执行以下步骤："
  echo
  echo "  # 第 2 步 派单"
  echo "  ( cd $SAFKC_ROOT && python -m super_agent.dispatch_cli \\"
  echo "      ${MODEL_ARGS[*]} \\"
  echo "      --cwd $CWD \\"
  echo "      --name $NAME \\"
  echo "      --persona \"<章程 $PERSONA_NOTE 的正文>\" \\"
  echo "      --task \"\$(cat $TASK_FILE)\" \\"
  echo "      --permission-mode auto --detach"
  echo
  echo "  # 第 3 步 解析 session id（从输出匹配 '[dispatch] detached: session id <sid>'）"
  echo
  echo "  # 第 4 步 同轮留痕"
  echo "  python -m super_agent.issue_cli bind --role pm --project $PROJECT --issue $ISSUE \\"
  echo "      --bind-role $BIND_ROLE --session <sid> --name $NAME"
  echo "  python -m super_agent.issue_cli label --role pm --project $PROJECT --issue $ISSUE --add $STAGE_LABEL"
  echo "  bash super_agent/scripts/pmc.sh --project $PROJECT --issue $ISSUE <临时评论文件> \$PM_KEY"
  echo "    # 评论＝摘要行 ＋ --- ＋ <details><summary>任务书全文</summary> ＋ 任务书全文 ${TASK_BYTES} 字节"
  echo "    # 摘要：${COMMENT_SUMMARY//<待解析>/<sid前12>…}"
  echo "    # 自检三条：任务书首行非空（首行为空则 grep -F \"\" 恒真、整段空转）／评论命中非空行 ≥ min(6,N)／评论字节 ≥ 任务书 90%；任一不过 ⇒ die，不发「只有摘要」的假成功评论"
  exit 0
fi

# ---------- 第 3 步：派单并解析 session id ----------
step "派单：$NAME（role=$ROLE, cwd=$CWD）"
DISPATCH_RAW="$( cd "$SAFKC_ROOT" && "${DISPATCH_CMD[@]}" 2>&1 )"
DISPATCH_RC=$?
printf '%s\n' "$DISPATCH_RAW" | sed 's/^/[dispatch] /'
[ "$DISPATCH_RC" -eq 0 ] || die "dispatch 失败（rc=$DISPATCH_RC）。已完成步骤：前置检查"

SID="$(printf '%s\n' "$DISPATCH_RAW" | sed -n 's/.*detached: session id \([0-9A-Za-z._-]*\).*/\1/p' | head -1)"
if [ -z "$SID" ]; then
  die "从 dispatch 输出解析不到 session id。已完成步骤：前置检查、派单（$NAME 已起，请手工 bind）"
fi
echo "[pmd] session=$SID"

SID_SHORT="$(printf '%s' "$SID" | head -c 12)"

# ---------- 第 4 步：同轮留痕 ----------
step "绑会话"
if ! ( cd "$SAFKC_ROOT" && python -m super_agent.issue_cli bind --role pm --project "$PROJECT" --issue "$ISSUE" \
      --bind-role "$BIND_ROLE" --session "$SID" --name "$NAME" ) 2>&1 | sed 's/^/  /'; then
  die "bind 失败。已完成步骤：前置检查、派单（session=$SID）"
fi

step "打 label：$STAGE_LABEL"
if ! ( cd "$SAFKC_ROOT" && python -m super_agent.issue_cli label --role pm --project "$PROJECT" --issue "$ISSUE" \
      --add "$STAGE_LABEL" ) 2>&1 | sed 's/^/  /'; then
  die "label 失败。已完成步骤：前置检查、派单（session=$SID）、bind"
fi

step "发带标记的评论（摘要 + 任务书全文 ${TASK_BYTES} 字节留档）"
COMMENT_SUMMARY="${COMMENT_SUMMARY//<待解析>/"${SID_SHORT}…"}"
TMP_COMMENT="$(mktemp)"
pmd_build_comment "$TASK_FILE" "$TMP_COMMENT" "$COMMENT_SUMMARY"

# 自检：全文必须真的进了评论（防「只发摘要冒充成功」）。
# 评论是永久留档，所以这条一律生效，没有精简模式。
if ! pmd_selfcheck_comment "$TASK_FILE" "$TMP_COMMENT"; then
  rm -f "$TMP_COMMENT"
  die "评论正文不是任务书全文，拒绝发送（已完成步骤：前置检查、派单、bind、label）"
fi

if ! bash "$SELF_DIR/pmc.sh" --project "$PROJECT" --issue "$ISSUE" "$TMP_COMMENT" "$PM_KEY" 2>&1 | sed 's/^/  /'; then
  rm -f "$TMP_COMMENT"
  die "评论失败。已完成步骤：前置检查、派单（session=$SID）、bind、label"
fi
rm -f "$TMP_COMMENT"

echo "[pmd] 完成：#$ISSUE $ROLE session=$SID name=$NAME label=$STAGE_LABEL"
