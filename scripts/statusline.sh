#!/bin/bash
# Fast (<75ms) TrueColor statusLine renderer for Antigravity CLI (`agy`).
# Design incorporates Lumidarin/agy-capsule visual hierarchy & live telemetry:
#   - Styles: capsule (subtle dark glass pill) | powerline (Material 800 solid) | pill (accent bar) | minimal (plain)
#   - Indicators: ring (○◔◑◕●) | bar (6-cell ■□)
#   - Color modes: row (uniform accent/color per row) | semantic (status/threshold colored)
#   - Live telemetry from active transcript.jsonl tail (<2ms): Prompt Cache hit + 3m/4m TTL + live Token speed (t/s)

[ "${FAIZ_STATUSLINE_SILENT:-0}" = "1" ] && exit 0

CACHE_DIR="$HOME/.agents/cache"
USAGE_CACHE="$CACHE_DIR/usage_snapshot.json"
CLOUD_CACHE="$CACHE_DIR/cloud_snapshot.json"
SPEED_CACHE="$CACHE_DIR/speed_snapshot.json"
CONFIG_FILE="${FAIZ_STATUSLINE_CONFIG:-$HOME/.agents/statusline_config.json}"
DASH_BIN="${FAIZ_DASH_BIN:-$HOME/.local/bin/faiz-dash}"
JQ_BIN="/usr/bin/jq"
[ -x "$JQ_BIN" ] || JQ_BIN="$(command -v jq || true)"

_file_mtime() {
    local f="$1"
    [ -s "$f" ] || { echo 0; return; }
    stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null || echo 0
}

_sniff_term_width() {
    if [ -n "${FAIZ_TERM_WIDTH:-}" ] && [ "${FAIZ_TERM_WIDTH}" -gt 0 ] 2>/dev/null; then
        term_cols="$FAIZ_TERM_WIDTH"
        return
    fi
    if [ -n "${COLUMNS:-}" ] && [ "${COLUMNS}" -gt 0 ] 2>/dev/null; then
        term_cols="$COLUMNS"
        return
    fi
    if [[ "$stdin_payload" =~ \"terminal_width\"[[:space:]]*:[[:space:]]*([0-9]+) ]] && [ "${BASH_REMATCH[1]}" -gt 0 ] 2>/dev/null; then
        term_cols="${BASH_REMATCH[1]}"
        return
    fi
    local rex_blk="${REX_BLOCK_ID:-${REX_BLOCK:-}}"
    if [ -n "$rex_blk" ] && command -v rex >/dev/null 2>&1; then
        local r_cols
        r_cols=$(rex --timeout 1s do -b "$rex_blk" com.superlogical.terminal.size 2>/dev/null | "$JQ_BIN" -r '.columns // empty' 2>/dev/null || true)
        if [ -n "$r_cols" ] && [ "$r_cols" -gt 0 ] 2>/dev/null; then
            term_cols="$r_cols"
            return
        fi
    fi
    if [ -r /dev/tty ]; then
        local tty_sz tty_cols
        tty_sz=$(stty size </dev/tty 2>/dev/null || true)
        tty_cols="${tty_sz##* }"
        if [ -n "$tty_cols" ] && [ "$tty_cols" -gt 0 ] 2>/dev/null && [ "$tty_sz" != "24 80" ]; then
            term_cols="$tty_cols"
            return
        fi
    fi
    term_cols=90
}

_detect_truecolor() {
    local ct="${COLORTERM:-}"
    local tm="${TERM:-}"
    local tp="${TERM_PROGRAM:-}"
    case "$ct" in
        *truecolor*|*24bit*) has_tc="true"; return ;;
    esac
    case "$tm" in
        *kitty*|*ghostty*|*direct*) has_tc="true"; return ;;
    esac
    case "$tp" in
        rex|ghostty|iTerm.app|WezTerm|kitty) has_tc="true"; return ;;
        Apple_Terminal) has_tc="false"; return ;;
    esac
    case "$tm" in
        *256color*) has_tc="true"; return ;;
    esac
    has_tc="false"
}

stdin_payload="{}"
if [ ! -t 0 ]; then
    IFS= read -r -d '' stdin_raw || true
    [ -n "$stdin_raw" ] && stdin_payload="$stdin_raw"
fi

_sniff_term_width
_detect_truecolor

# Resolve live transcript tail (FAIZ_TRANSCRIPT_PATH -> corrected stdin path -> conversation_id -> latest CLI brain transcript)
t_path="${FAIZ_TRANSCRIPT_PATH:-}"
if [ -z "$t_path" ] && [ -z "${FAIZ_TRANSCRIPT_PATH+x}" ]; then
    if [[ "$stdin_payload" =~ \"transcript_path\"[[:space:]]*:[[:space:]]*\"([^\"]+)\" ]]; then
        t_in="${BASH_REMATCH[1]}"
        if [ -s "$t_in" ]; then
            t_path="$t_in"
        else
            t_fix="${t_in/\/.gemini\/antigravity\//\/.gemini\/antigravity-cli\/}"
            [ -s "$t_fix" ] && t_path="$t_fix"
        fi
    fi
    if [ -z "$t_path" ] && [[ "$stdin_payload" =~ \"conversation_id\"[[:space:]]*:[[:space:]]*\"([^\"]+)\" ]]; then
        t_cid="${BASH_REMATCH[1]}"
        if [ -n "$t_cid" ]; then
            t_cand="$HOME/.gemini/antigravity-cli/brain/$t_cid/.system_generated/logs/transcript.jsonl"
            [ -s "$t_cand" ] && t_path="$t_cand"
        fi
    fi
    if [ -z "$t_path" ] && [ -d "$HOME/.gemini/antigravity-cli/brain" ]; then
        t_latest=$(ls -1t "$HOME"/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl 2>/dev/null | head -n 1 || true)
        [ -n "$t_latest" ] && [ -s "$t_latest" ] && t_path="$t_latest"
    fi
fi

# Single stat pass across cache, transcript & bg stamps
u_mt=0; c_mt=0; t_mtime=0; t_sz=0; s_mtime=0; p_mt=0; c_st_mt=0
pty_stamp="$CACHE_DIR/.bg_pty_ts"
cloud_stamp="$CACHE_DIR/.bg_cloud_ts"
backoff_file="$CACHE_DIR/cloud_backoff.json"
stat_files=()
[ -s "$USAGE_CACHE" ] && stat_files+=("$USAGE_CACHE")
[ -s "$CLOUD_CACHE" ] && stat_files+=("$CLOUD_CACHE")
[ -n "$t_path" ] && [ -s "$t_path" ] && stat_files+=("$t_path")
if [ "${FAIZ_STATUSLINE_NO_BG:-0}" != "1" ]; then
    [ -s "$SPEED_CACHE" ] && stat_files+=("$SPEED_CACHE")
    [ -e "$pty_stamp" ] && stat_files+=("$pty_stamp")
    [ -e "$cloud_stamp" ] && stat_files+=("$cloud_stamp")
fi
if [ "${#stat_files[@]}" -gt 0 ]; then
    stat_out=$(stat -f $'%m\t%z\t%N' "${stat_files[@]}" 2>/dev/null || stat -c $'%Y\t%s\t%n' "${stat_files[@]}" 2>/dev/null || true)
    while IFS=$'\t' read -r sm sz sn; do
        case "$sn" in
            "$USAGE_CACHE") u_mt="$sm" ;;
            "$CLOUD_CACHE") c_mt="$sm" ;;
            "$t_path") t_mtime="$sm"; t_sz="$sz" ;;
            "$SPEED_CACHE") s_mtime="$sm" ;;
            "$pty_stamp") p_mt="$sm" ;;
            "$cloud_stamp") c_st_mt="$sm" ;;
        esac
    done <<< "$stat_out"
fi

if [ -n "$t_path" ] && [ -s "$t_path" ] && [ "${FAIZ_STATUSLINE_NO_BG:-0}" != "1" ] && [ "$t_mtime" -gt "$s_mtime" ]; then
    (
        latest_done=$(tail -n 1 "$t_path" 2>/dev/null | "$JQ_BIN" -c --argjson tmt "$t_mtime" '
          select(.type == "PLANNER_RESPONSE" and .status == "DONE" and (.output_tokens // 0) > 0 and (.created_at // "" | length >= 19)) |
          (.created_at[0:19] + "Z" | fromdateiso8601? // null) as $ts |
          select($ts != null) | ($tmt - $ts) as $dt |
          select($dt >= 1 and $dt <= 180) |
          {step_index: (.step_index // 0), tps: ((.output_tokens / $dt) | round)}
        ' 2>/dev/null || true)
        if [ -n "$latest_done" ]; then
            mkdir -p "$CACHE_DIR" 2>/dev/null || true
            printf '%s\n' "$latest_done" > "$SPEED_CACHE.tmp.$$" 2>/dev/null && mv -f "$SPEED_CACHE.tmp.$$" "$SPEED_CACHE" 2>/dev/null || true
        fi
    ) </dev/null >/dev/null 2>&1 &
fi

# Debounced non-blocking background refresh (never blocks jq rendering)
if [ "${FAIZ_STATUSLINE_NO_BG:-0}" != "1" ] && [ -x "$DASH_BIN" ]; then
    now_ts=$(date +%s 2>/dev/null || echo 0)
    p_last=$([ "$p_mt" -gt 0 ] && echo $((now_ts - p_mt)) || echo 999999)
    c_age=$([ "$c_mt" -gt 0 ] && echo $((now_ts - c_mt)) || echo 999999)
    c_last=$([ "$c_st_mt" -gt 0 ] && echo $((now_ts - c_st_mt)) || echo 999999)
    need_pty=0
    need_cloud=0
    pty_cooldown=180
    if [ "$t_mtime" -gt 0 ] && [ $((now_ts - t_mtime)) -lt 120 ]; then
        pty_cooldown=20
    fi
    if [ "$u_mt" -eq 0 ] || [ "$p_last" -gt "$pty_cooldown" ]; then
        need_pty=1
    fi
    [ "$c_age" -gt 90 ] && [ "$c_last" -gt 90 ] && need_cloud=1
    if [ "$need_cloud" -eq 1 ] && [ ! -s "${RCLONE_CONFIG:-$HOME/.config/rclone/rclone.conf}" ] && [ "${FAIZ_ENABLE_CLOUD:-0}" != "1" ]; then
        need_cloud=0
    fi
    if [ "$need_cloud" -eq 1 ] && [ -s "$backoff_file" ]; then
        b_raw=$(< "$backoff_file")
        if [[ "$b_raw" =~ \"next_allowed_epoch\"[[:space:]]*:[[:space:]]*([0-9]+) ]] && [ "${BASH_REMATCH[1]}" -gt "$now_ts" ] 2>/dev/null; then
            need_cloud=0
        fi
    fi

    if [ "$need_pty" -eq 1 ] || [ "$need_cloud" -eq 1 ]; then
        mkdir -p "$CACHE_DIR" 2>/dev/null || true
        lock_d="$CACHE_DIR/bg_refresh.lock.d"
        if [ -d "$lock_d" ]; then
            l_mt=$(_file_mtime "$lock_d")
            [ "$l_mt" -gt 0 ] && [ $((now_ts - l_mt)) -gt 45 ] && rmdir "$lock_d" 2>/dev/null || true
        fi
        if mkdir "$lock_d" 2>/dev/null; then
            if [ "$need_pty" -eq 1 ] && [ "$need_cloud" -eq 1 ]; then
                touch "$pty_stamp" "$cloud_stamp" 2>/dev/null || true
                ( FAIZ_NO_DASH=1 "$DASH_BIN" --once-json >/dev/null 2>&1; rmdir "$lock_d" 2>/dev/null || true ) </dev/null >/dev/null 2>&1 &
            elif [ "$need_pty" -eq 1 ]; then
                touch "$pty_stamp" 2>/dev/null || true
                ( FAIZ_NO_DASH=1 "$DASH_BIN" --once-json --quota-only >/dev/null 2>&1; rmdir "$lock_d" 2>/dev/null || true ) </dev/null >/dev/null 2>&1 &
            elif [ "$need_cloud" -eq 1 ]; then
                touch "$cloud_stamp" 2>/dev/null || true
                ( FAIZ_NO_DASH=1 "$DASH_BIN" --once-json --no-pty >/dev/null 2>&1; rmdir "$lock_d" 2>/dev/null || true ) </dev/null >/dev/null 2>&1 &
            else
                rmdir "$lock_d" 2>/dev/null || true
            fi
        fi
    fi
fi

# Incremental single-session total tokens (input + output) from active transcript.jsonl
sess_tok="null"
if [ -n "$t_path" ] && [ -s "$t_path" ] && [ "$t_sz" -gt 0 ] 2>/dev/null; then
    BURN_CACHE="$CACHE_DIR/.session_burn"
    sb_path=""; sb_off=0; sb_tok=0
    if [ -s "$BURN_CACHE" ]; then
        IFS=$'\t' read -r sb_path sb_off sb_tok < "$BURN_CACHE" 2>/dev/null || true
    fi
    if [ "$sb_path" != "$t_path" ] || [ "$t_sz" -lt "${sb_off:-0}" ] 2>/dev/null; then
        sb_off=0
        sb_tok=0
    fi
    sb_off="${sb_off:-0}"
    sb_tok="${sb_tok:-0}"
    if [ "$t_sz" -gt "$sb_off" ]; then
        delta_res=$(tail -c +"$((sb_off + 1))" "$t_path" 2>/dev/null | "$JQ_BIN" -Rrs '
          endswith("\n") as $full
          | (if $full then . else ((rindex("\n")) as $idx | if $idx == null then "" else .[0:($idx + 1)] end) end) as $body
          | ($body | split("\n") | map(select(length > 0) | (try (fromjson | select(.type == "PLANNER_RESPONSE") | ((.input_tokens // 0) + (.output_tokens // 0))) catch 0)) | add // 0) as $add
          | "\($add)\t\(if $full then 1 else 0 end)"
        ' 2>/dev/null || true)
        IFS=$'\t' read -r add_tok is_full <<< "$delta_res"
        if [ -n "$add_tok" ]; then
            case "$add_tok:$is_full" in
                *[!0-9:]*|:*) ;;
                *:0|*:1)
                    sb_tok=$((sb_tok + add_tok))
                    if [ "$is_full" = "1" ]; then
                        mkdir -p "$CACHE_DIR" 2>/dev/null || true
                        printf '%s\t%s\t%s\n' "$t_path" "$t_sz" "$sb_tok" > "$BURN_CACHE.tmp.$$" 2>/dev/null && mv -f "$BURN_CACHE.tmp.$$" "$BURN_CACHE" 2>/dev/null || true
                    fi
                    ;;
            esac
        fi
    fi
    sess_tok="$sb_tok"
fi

jq_args=(-rn --argjson in "$stdin_payload" --argjson u_mt "$u_mt" --argjson c_mt "$c_mt" --argjson term_cols "$term_cols" --argjson has_tc "$has_tc" --argjson t_mtime "$t_mtime" --argjson sess_tok "$sess_tok")
if [ -s "$USAGE_CACHE" ]; then
    jq_args+=(--slurpfile u "$USAGE_CACHE")
else
    jq_args+=(--argjson u '[]')
fi
if [ -s "$CLOUD_CACHE" ]; then
    jq_args+=(--slurpfile c "$CLOUD_CACHE")
else
    jq_args+=(--argjson c '[]')
fi
if [ -s "$SPEED_CACHE" ]; then
    jq_args+=(--slurpfile spd "$SPEED_CACHE")
else
    jq_args+=(--argjson spd '[]')
fi
cfg_raw=""
if [ -s "$CONFIG_FILE" ]; then
    cfg_raw=$(< "$CONFIG_FILE")
    jq_args+=(--slurpfile cfg "$CONFIG_FILE")
else
    jq_args+=(--argjson cfg '[]')
fi

case "$cfg_raw" in
    *'"nord"'*)
        p_tbl='{"glass_bg":"46;52;64","glass_lbl":"152;163;184","glass_val":"236;239;244","base_bg":"59;66;82","base_fg":"229;233;240","base_acc":"136;192;208","ctx_bg":"46;64;87","ctx_fg":"236;239;244","ctx_acc":"136;192;208","ok_bg":"46;74;60","ok_fg":"236;239;244","ok_acc":"163;190;140","warn_bg":"78;62;38","warn_fg":"242;212;152","warn_acc":"235;203;139","alert_bg":"84;42;52","alert_fg":"248;220;224","alert_acc":"191;97;106","muted_bg":"49;55;68","muted_fg":"165;175;194","muted_acc":"140;152;174"}' ;;
    *'"tokyo_night"'*)
        p_tbl='{"glass_bg":"36;40;59","glass_lbl":"136;148;186","glass_val":"205;214;248","base_bg":"41;46;66","base_fg":"205;214;248","base_acc":"122;162;247","ctx_bg":"38;61;92","ctx_fg":"205;214;248","ctx_acc":"125;207;255","ok_bg":"36;66;55","ok_fg":"205;214;248","ok_acc":"158;206;106","warn_bg":"74;58;32","warn_fg":"236;188;118","warn_acc":"224;175;104","alert_bg":"82;38;52","alert_fg":"255;212;222","alert_acc":"247;118;142","muted_bg":"38;43;61","muted_fg":"146;158;194","muted_acc":"110;122;158"}' ;;
    *'"catppuccin"'*)
        p_tbl='{"glass_bg":"49;50;68","glass_lbl":"166;173;200","glass_val":"205;214;244","base_bg":"62;64;86","base_fg":"217;226;252","base_acc":"137;180;250","ctx_bg":"42;68;86","ctx_fg":"217;226;252","ctx_acc":"137;220;235","ok_bg":"42;74;56","ok_fg":"217;226;252","ok_acc":"166;227;161","warn_bg":"80;66;38","warn_fg":"252;232;186","warn_acc":"249;226;175","alert_bg":"86;40;56","alert_fg":"252;220;230","alert_acc":"243;139;168","muted_bg":"50;52;70","muted_fg":"166;173;200","muted_acc":"147;153;178"}' ;;
    *'"custom"'*)
        p_tbl='null' ;;
    *)
        p_tbl='{"glass_bg":"48;51;58","glass_lbl":"172;180;194","glass_val":"238;240;244","ghost_brd":"107;114;128","base_bg":"60;64;67","base_fg":"232;234;237","base_acc":"138;180;248","base_gbg":"48;51;58","base_glbl":"172;180;194","base_gval":"238;240;244","ctx_bg":"23;78;166","ctx_fg":"248;249;250","ctx_acc":"138;180;248","ctx_gbg":"35;45;64","ctx_glbl":"156;172;198","ctx_gval":"238;243;252","ok_bg":"13;101;45","ok_fg":"248;249;250","ok_acc":"129;201;149","ok_gbg":"27;52;38","ok_glbl":"162;204;178","ok_gval":"234;248;239","warn_bg":"251;188;4","warn_fg":"32;33;36","warn_acc":"250;204;107","warn_gbg":"60;50;26","warn_glbl":"214;196;150","warn_gval":"252;218;140","alert_bg":"165;14;14","alert_fg":"248;249;250","alert_acc":"242;139;130","alert_gbg":"64;30;31","alert_glbl":"218;174;174","alert_gval":"250;228;226","muted_bg":"52;56;62","muted_fg":"189;193;198","muted_acc":"154;160;166","muted_gbg":"42;45;52","muted_glbl":"158;164;174","muted_gval":"218;222;228"}' ;;
esac
jq_args+=(--argjson p_tbl "$p_tbl")

case "$cfg_raw" in
    *'"zh"'*)
        JQ_DISP_LEN='def disp_len: if (test("[\u2e80-\u9fff\uac00-\ud7af\uf900-\uffff]") | not) then length else explode | reduce .[] as $cp (0; . + (if ($cp >= 11904 and $cp <= 40959) or ($cp >= 4352 and $cp <= 4447) or ($cp >= 44032 and $cp <= 55203) or ($cp >= 63744 and $cp <= 65500) then 2 else 1 end)) end;' ;;
    *)
        JQ_DISP_LEN='def disp_len: length;' ;;
esac

JQ_FILTER="${JQ_DISP_LEN}"'
  def ansi16($name):
    ({
      "cyan": "\u001b[36m", "green": "\u001b[32m", "yellow": "\u001b[33m",
      "red": "\u001b[31m", "blue": "\u001b[34m", "magenta": "\u001b[35m",
      "white": "\u001b[37m", "gray": "\u001b[90m",
      "bright_cyan": "\u001b[96m", "bright_green": "\u001b[92m",
      "bright_yellow": "\u001b[93m", "bright_red": "\u001b[91m",
      "bright_blue": "\u001b[94m", "bright_magenta": "\u001b[95m"
    }[$name] // "");
  def slot_rgb($name):
    ({
      "cyan": "136;192;208", "green": "163;190;140", "yellow": "235;203;139",
      "red": "191;97;106", "blue": "129;161;193", "magenta": "180;142;173",
      "white": "229;233;240", "gray": "140;149;166",
      "bright_cyan": "143;188;187", "bright_green": "166;227;161",
      "bright_yellow": "249;226;175", "bright_red": "243;139;168",
      "bright_blue": "137;180;250", "bright_magenta": "203;166;247"
    }[$name] // "216;222;233");
  def theme_palette($t; $custom_cols):
    if $p_tbl != null then $p_tbl
    else {
      glass_bg: "42;45;51", glass_lbl: "154;160;166", glass_val: "232;234;237",
      base_bg: "59;66;82", base_fg: "229;233;240", base_acc: "136;192;208",
      ctx_bg: "46;64;87", ctx_fg: slot_rgb($custom_cols.context // "cyan"), ctx_acc: slot_rgb($custom_cols.context // "cyan"),
      ok_bg: "46;74;60", ok_fg: slot_rgb($custom_cols.ok // "green"), ok_acc: slot_rgb($custom_cols.ok // "green"),
      warn_bg: "78;62;38", warn_fg: slot_rgb($custom_cols.warn // "yellow"), warn_acc: slot_rgb($custom_cols.warn // "yellow"),
      alert_bg: "84;42;52", alert_fg: slot_rgb($custom_cols.alert // "red"), alert_acc: slot_rgb($custom_cols.alert // "red"),
      muted_bg: "49;55;68", muted_fg: slot_rgb($custom_cols.muted // "gray"), muted_acc: slot_rgb($custom_cols.muted // "gray")
    } end;

  def rst: "\u001b[0m";
  def fmt1:
    (((. * 10) | round) / 10) as $v |
    ($v | tostring) as $s |
    if ($s | contains(".")) then $s else "\($s).0" end;
  def fmt2:
    (((. * 100) | round)) as $c |
    (($c / 100) | floor) as $w |
    ($c - ($w * 100)) as $f |
    "\($w)." + (if $f < 10 then "0\($f)" else "\($f)" end);
  def pct_str($v; $short):
    if $short then "\($v | round)" else ($v | fmt1) end;
  def quota_str($v; $prec; $tight):
    if $prec == "int" or $tight then "\($v | round)"
    elif $prec == "1dp" then ($v | fmt1)
    else ($v | fmt2) end;
  def bar6($pct):
    (($pct / 16.6667) | round) as $r |
    (if $r < 0 then 0 elif $r > 6 then 6 else $r end) as $f |
    (("■■■■■■"[0:$f]) + ("□□□□□□"[0:(6 - $f)]));
  def ring($pct):
    if $pct >= 88 then "●"
    elif $pct >= 63 then "◕"
    elif $pct >= 38 then "◑"
    elif $pct >= 13 then "◔"
    else "○" end;

  def row_token_to_slot($tok):
    ({
      "blue": "ctx",
      "green": "ok",
      "yellow": "warn",
      "red": "alert",
      "slate": "base",
      "muted": "muted"
    }[$tok] // "base");

  def slot_style($pal; $slot; $c16):
    if $slot == "ctx" then [$pal.ctx_bg, $pal.ctx_fg, $pal.ctx_acc, $c16.context, ($pal.ctx_gbg // $pal.ctx_bg), ($pal.ctx_glbl // $pal.glass_lbl), ($pal.ctx_gval // $pal.glass_val)]
    elif $slot == "ok" then [$pal.ok_bg, $pal.ok_fg, $pal.ok_acc, $c16.ok, ($pal.ok_gbg // $pal.ok_bg), ($pal.ok_glbl // $pal.glass_lbl), ($pal.ok_gval // $pal.glass_val)]
    elif $slot == "warn" then [$pal.warn_bg, $pal.warn_fg, $pal.warn_acc, $c16.warn, ($pal.warn_gbg // $pal.warn_bg), ($pal.warn_glbl // $pal.glass_lbl), ($pal.warn_gval // $pal.glass_val)]
    elif $slot == "alert" then [$pal.alert_bg, $pal.alert_fg, $pal.alert_acc, $c16.alert, ($pal.alert_gbg // $pal.alert_bg), ($pal.alert_glbl // $pal.glass_lbl), ($pal.alert_gval // $pal.glass_val)]
    elif $slot == "muted" then [$pal.muted_bg, $pal.muted_fg, $pal.muted_acc, $c16.muted, ($pal.muted_gbg // $pal.muted_bg), ($pal.muted_glbl // $pal.glass_lbl), ($pal.muted_gval // $pal.glass_val)]
    else [$pal.base_bg, $pal.base_fg, $pal.base_acc, "", ($pal.base_gbg // $pal.base_bg), ($pal.base_glbl // $pal.glass_lbl), ($pal.base_gval // $pal.glass_val)]
    end;

  def repeat_str($ch; $n):
    if $n <= 0 then "" else ($ch * $n) end;

  def item_plain($it):
    (if $it.icon != "" then "\($it.icon) " else "" end) + ($it.parts | map(.t) | join(""));

  def cap_width($it; $style):
    (item_plain($it) | disp_len) + ($it.pad // 0) + (if $style == "minimal" then 0 elif $style == "ghost" then 2 else 4 end);

  def render_capsule($it; $sp; $acc_sp; $pal; $style; $tc; $tint_val):
    item_plain($it) as $plain |
    repeat_str(" "; ($it.pad // 0)) as $pad_str |
    if ($style == "minimal") or ($tc | not) then
      (if $sp[3] != "" then "\($sp[3])\($plain)\(rst)" else $plain end) + $pad_str
    elif $style == "ghost" then
      ($pal.ghost_brd // "107;114;128") as $brd |
      (if $tint_val then $acc_sp[2] else $pal.glass_val end) as $vcol |
      (
        ($it.parts | map(
          "\u001b[38;2;\(if .r == "lbl" then $pal.glass_lbl elif .r == "acc" then $acc_sp[2] else $vcol end)m\(.t)"
        ) | join(""))
      ) as $body |
      (if $it.icon != "" then "\u001b[38;2;\($acc_sp[2])m\($it.icon) " else "" end) as $ipfx |
      "\u001b[38;2;\($brd)m[\($ipfx)\($body)\($pad_str)\u001b[38;2;\($brd)m]\(rst)"
    elif $style == "capsule" then
      ($sp[4]) as $gbg |
      ($sp[5]) as $glbl |
      (if $tint_val then $acc_sp[2] else $sp[6] end) as $vcol |
      (
        ($it.parts | map(
          "\u001b[38;2;\(if .r == "lbl" then $glbl elif .r == "acc" then $acc_sp[2] else $vcol end)m\(.t)"
        ) | join(""))
      ) as $body |
      (if $it.icon != "" then "\u001b[38;2;\($acc_sp[2])m\($it.icon) " else "" end) as $ipfx |
      "\u001b[38;2;\($gbg)m\ue0b6\u001b[48;2;\($gbg)m \($ipfx)\($body)\($pad_str) \u001b[0;38;2;\($gbg)m\ue0b4\(rst)"
    elif $style == "powerline" then
      "\u001b[38;2;\($sp[0])m\ue0b6\u001b[48;2;\($sp[0]);38;2;\($sp[1])m \($plain)\($pad_str) \u001b[0;38;2;\($sp[0])m\ue0b4\(rst)"
    else
      "\u001b[48;2;\($sp[0]);38;2;\($sp[2])m▎\u001b[38;2;\($sp[1])m \($plain)\($pad_str) \(rst)"
    end;

  def pack_rows($items; $max_w; $do_wrap; $sep_w; $style):
    if ($items | length) == 0 then []
    elif ($do_wrap | not) or $max_w <= 0 then [$items]
    else
      reduce $items[] as $it (
        {rows: [], cur: [], cur_w: 0};
        (cap_width($it; $style)) as $iw |
        if (.cur | length) == 0 then
          .cur = [$it] | .cur_w = $iw
        elif (.cur_w + $sep_w + $iw) <= $max_w then
          .cur += [$it] | .cur_w = (.cur_w + $sep_w + $iw)
        else
          .rows += [.cur] | .cur = [$it] | .cur_w = $iw
        end
      ) | (.rows + (if (.cur | length) > 0 then [.cur] else [] end))
    end;

  def expand_col_ws($col_ws; $max_w; $sep_w):
    (($col_ws | add) + ((($col_ws | length) - 1) * $sep_w)) as $cur_w |
    (if $max_w > 210 then 210 else $max_w end) as $cap_w |
    ($cap_w - $cur_w) as $extra |
    if $extra <= 0 or (($col_ws | length) == 0) then $col_ws
    else
      reduce range(0; $extra) as $_ (
        $col_ws;
        min as $m | (indices($m) | .[0]) as $mi | .[$mi] += 1
      )
    end;

  def pack_matrix_rows($items; $max_w; $do_wrap; $sep_w; $style; $expand_w):
    if ($items | length) < 5 or ($do_wrap | not) or $max_w <= 0 then
      pack_rows($items; $max_w; $do_wrap; $sep_w; $style)
    else
      ((($items | length) + 1) / 2 | floor) as $cols |
      ($items[0:$cols]) as $r0 |
      ($items[$cols:]) as $r1 |
      [
        range(0; $cols) as $c |
        [
          (if $c < ($r0 | length) then cap_width($r0[$c]; $style) else 0 end),
          (if $c < ($r1 | length) then cap_width($r1[$c]; $style) else 0 end)
        ] | max
      ] as $raw_ws |
      (($raw_ws | add) + (($cols - 1) * $sep_w)) as $total_w |
      if $total_w <= $max_w then
        (if $expand_w then expand_col_ws($raw_ws; $max_w; $sep_w) else $raw_ws end) as $col_ws |
        [
          [range(0; ($r0 | length)) as $c | $r0[$c] + {pad: ($col_ws[$c] - cap_width($r0[$c]; $style))}],
          [range(0; ($r1 | length)) as $c | $r1[$c] + {pad: ($col_ws[$c] - cap_width($r1[$c]; $style))}]
        ]
      else
        pack_rows($items; $max_w; $do_wrap; $sep_w; $style)
      end
    end;

  ($u[0] // {}) as $u |
  ($c[0] // {}) as $c |
  ($spd[0] // {}) as $spd |
  ($cfg[0] // {}) as $raw_cfg |

  ($raw_cfg.language // "en") as $lang |
  ($raw_cfg.style // "capsule") as $req_style |
  ($raw_cfg.indicator // "ring") as $ind |
  ($raw_cfg.density // "auto") as $density |
  ($raw_cfg.secondary_metric // "burn") as $sec_metric |
  ($raw_cfg.theme // "google") as $theme |
  ($raw_cfg.color_mode // "row") as $color_mode |
  ($raw_cfg.quota_precision // "exact") as $q_prec |
  (($raw_cfg.row_colors // ["blue", "green", "yellow", "red"]) | if length > 0 then . else ["blue", "green", "yellow", "red"] end) as $row_colors |
  (if ($raw_cfg.top_gap == false) then false else true end) as $top_gap |
  (if ($raw_cfg.line_gap == false) then false else true end) as $line_gap |
  (if $has_tc then $req_style else "minimal" end) as $eff_style |
  (if ($raw_cfg.auto_wrap == false) then false else true end) as $auto_wrap |
  ((if ($raw_cfg.max_width // 0) > 0 then $raw_cfg.max_width else $term_cols end) - 2) as $eff_cols |
  ($density == "compact") as $use_compact |
  ($density == "auto") as $use_adaptive_2row |
  ($use_adaptive_2row and ($eff_cols >= 155)) as $wide_rich |
  ($use_adaptive_2row and ($eff_cols <= 95)) as $short_pct |
  ($eff_cols < 82) as $tight_quota |

  ($raw_cfg.modules // {}) as $m |
  (if ($m.context == false) then false else true end) as $m_ctx |
  (if ($m.cache == false) then false else true end) as $m_cache |
  (if ($m.speed == false) then false else true end) as $m_speed |
  (if ($m.quota == false) then false else true end) as $m_quota |
  (if ($m.sessions == false) then false else true end) as $m_sess |
  (if ($m.colab == false) then false else true end) as $m_colab |
  (if ($m.mailbox == false) then false else true end) as $m_mbox |
  (if ($m.freshness == false) then false else true end) as $m_fresh |

  ($raw_cfg.colors // {}) as $col |
  theme_palette($theme; $col) as $pal |
  {
    context: ansi16($col.context // "cyan"),
    ok: ansi16($col.ok // "green"),
    warn: ansi16($col.warn // "yellow"),
    alert: ansi16($col.alert // "red"),
    muted: ansi16($col.muted // "gray")
  } as $c16 |

  (now | floor) as $now_ts |
  # Live transcript metrics: cache hit %, TTL idle age, and latest step speed (tok/s)
  ($raw_t | map(select(type == "object") | {
    idx: (.step_index // 0),
    type: (.type // ""),
    status: (.status // ""),
    ts: ((.created_at // "") | if length >= 19 then (.[0:19] + "Z" | fromdateiso8601? // null) else null end),
    inp: (.input_tokens // 0),
    cr: (.cache_read_tokens // 0),
    out: (.output_tokens // 0)
  })) as $tsteps |
  ([ $tsteps[] | select(.type == "PLANNER_RESPONSE" and (.inp + .cr) > 0) ] | last) as $last_tok |
  (if $last_tok != null then (($last_tok.cr / ($last_tok.inp + $last_tok.cr)) * 100) else null end) as $cache_hit |
  (if $last_tok != null and $last_tok.ts != null then ($now_ts - $last_tok.ts) else null end) as $cache_age |

  (
    [
      range(0; ($tsteps | length)) as $i |
      ($tsteps[$i]) as $s |
      select($s.type == "PLANNER_RESPONSE" and $s.status == "DONE" and ($s.out // 0) > 0 and $s.ts != null) |
      if ($i + 1) < ($tsteps | length) then
        ($tsteps[$i + 1]) as $nxt |
        if $nxt.type != "USER_INPUT" and $nxt.ts != null then
          ($nxt.ts - $s.ts) as $dt |
          if $dt >= 1 and $dt <= 300 then (($s.out / $dt) | round) else empty end
        elif ($spd.step_index // -1) == $s.idx and ($spd.tps // 0) > 0 then
          $spd.tps
        else empty end
      else
        ($t_mtime - $s.ts) as $dt |
        if $dt >= 1 and $dt <= 180 then (($s.out / $dt) | round)
        elif ($spd.step_index // -1) == $s.idx and ($spd.tps // 0) > 0 then $spd.tps
        else empty end
      end
    ] | last // null
  ) as $live_tps |

  ($in.context_window.used_percentage // null) as $ctx |
  ($u.pro_5h_pct // null) as $pro5 |
  ($u.pro_wk_pct // null) as $prowk |
  ($u.claude_5h_pct // null) as $cla5 |
  ($u.claude_wk_pct // null) as $cla |
  ($u.argon_wk_pct // null) as $arg |
  (($cla5 != null and $cla5 < 99.9) or ($cla != null and $cla < 99.9)) as $cla_show |
  ($use_adaptive_2row and ($eff_cols < 100)) as $cla_tight |
  (if $cla_show then
    (if $cla_tight or $tight_quota or ($q_prec == "int") then
      (if $cla5 != null and $cla != null then "\($cla5 | round)·\($cla | round)%"
       elif $cla != null then "\($cla | round)%"
       else "\($cla5 | round)%" end)
     else
      (if $cla5 != null then "\(quota_str($cla5; $q_prec; false))%" else "" end) as $c5p |
      (if $cla != null then "\(quota_str($cla; $q_prec; false))%" else "" end) as $cwp |
      (if $c5p != "" and $cwp != "" then "\($c5p)·\($cwp)"
       elif $cwp != "" then $cwp
       else $c5p end)
     end)
   else "" end) as $cla_val |
  (if $cla5 != null and $cla != null then (if $cla5 < $cla then $cla5 else $cla end)
   elif $cla5 != null then $cla5
   else $cla end) as $cla_min |
  ($u.quota_gate // "") as $gate |
  ($u.pro_count // null) as $pc |
  ($u.gcp_count // null) as $gc |
  ($u.tokens_1h // null) as $tok1h |
  ($u.pro_5h_reset_at_epoch // null) as $reset_ep |
  ($c.colab_state // "") as $clb |
  ($c.cloud_ok // ($clb != "DEGRADED" and $clb != "")) as $cloud_ok |
  ($c.cloud_error_kind // (if (($c.error // "") | test("RATE_LIMIT|rateLimit"; "i")) then "rate_limit" elif (($c.error // "") | test("TIMED OUT|TIMEOUT"; "i")) then "timeout" else "" end)) as $err_kind |
  ($c.idle_remaining_seconds // null) as $idle |
  ($c.gce_running_count // null) as $gce |
  ($c.mailbox_tasks_count // null) as $mt |
  ($c.mailbox_failed_count // null) as $mf |
  ($c.mailbox_unverified_count // 0) as $mu |
  ($c.mailbox.stale // false) as $m_stale |
  ($c.mailbox_done_count // 0) as $md |
  ($u.pty_fetched_epoch // (if $u_mt > 0 then $u_mt else 0 end)) as $pty_ep |
  (if $pty_ep > 0 then ($now_ts - $pty_ep) else 999999 end) as $pty_age |
  (if $c_mt > 0 then ($now_ts - $c_mt) else 999999 end) as $c_age |
  (if ($clb == "NOT_CONFIGURED" or $clb == "") then $pty_age else ([ $pty_age, $c_age ] | max) end) as $max_age |

'

JQ_COMPACT='
    [] |
    (
      if $m_ctx then
        if $ctx != null then
          (($ctx | round)) as $cp |
          (if $cp >= 90 then "alert" elif $cp >= 75 then "warn" else "ctx" end) as $cs |
          [
            (if $ind == "ring" then ring($cp) else "" end),
            ([{t: (if $lang == "en" then "Ctx " else "上下文 " end), r: "lbl"}, {t: "\($cp)%", r: "val"}] + (if $ind == "bar" then [{t: " \(bar6($cp))", r: "acc"}] else [] end)),
            $cs
          ]
        else
          ["◈", [{t: "Faiz", r: "val"}], "ctx"]
        end
      else null end
    ) as $c1 |
    (
      ($c1 // ["●", [], "ctx"]) as $base1 |
      (
        $base1[1] +
        (if $m_cache and ($cache_hit != null) then
          (if ($base1[1] | length) > 0 then [{t: "·", r: "lbl"}] else [] end) +
          [{t: (if $lang == "en" then "C" else "缓" end), r: "lbl"}, {t: "\($cache_hit | round)%", r: "val"}] +
          (if ($cache_age != null and $cache_age >= 240) then [{t: "!", r: "acc"}] else [] end)
         else [] end) +
        (if $m_speed and ($live_tps != null) and ($live_tps > 0) then
          [{t: "·", r: "lbl"}, {t: "\($live_tps)", r: "val"}, {t: "t/s", r: "lbl"}]
         else [] end)
      ) as $p1_parts |
      if ($p1_parts | length) > 0 then . + [{icon: $base1[0], parts: $p1_parts, slot: $base1[2], tint: ($base1[2] != "ctx")}] else . end
    ) |
    (
      if $m_quota then
        if $pro5 != null or $prowk != null or $arg != null then
          (
            [{t: "Gem ", r: "lbl"}, {t: "\(if $pro5 != null then quota_str($pro5; $q_prec; $tight_quota) else "--" end)/\(if $prowk != null then quota_str($prowk; $q_prec; $tight_quota) else "--" end)%", r: "val"}] +
            (if $arg != null then [{t: "·", r: "lbl"}, {t: (if ($arg < 20 or $gate == "THROTTLE_ARGON") then "▲" else "Arg " end), r: (if ($arg < 20 or $gate == "THROTTLE_ARGON") then "acc" else "lbl" end)}, {t: "\(quota_str($arg; $q_prec; $tight_quota))%\(if $gate == "WARN_PRO" or $gate == "THROTTLE_ARGON" then "!" else "" end)", r: "val"}] else [] end)
          ) as $p2_parts |
          (if $gate == "WARN_PRO" or ($pro5 != null and $pro5 < 20) then "alert"
           elif ($arg != null and $arg < 20) or ($gate == "THROTTLE_ARGON") or ($pro5 != null and $pro5 < 40) then "warn"
           else "base" end) as $q_slot |
          . + [{icon: (if $ind == "ring" and $pro5 != null then ring($pro5) else "" end), parts: $p2_parts, slot: $q_slot, tint: ($q_slot != "base")}]
        else
          . + [{icon: "○", parts: [{t: (if $lang == "en" then "Quota syncing" else "配额同步中" end), r: "lbl"}], slot: "muted", tint: false}]
        end
      else . end
    ) |
    (
      (if $m_sess and ($pc != null) then [{t: "\($pc)·\($gc // 0)", r: "val"}] else [] end) +
      (if $m_colab and ($clb != "") and ($clb != "NOT_CONFIGURED") then
        (if ($m_sess and ($pc != null)) then [{t: "·", r: "lbl"}] else [] end) +
        (if $clb == "ONLINE" then [{t: "Colab ", r: "lbl"}, {t: (if $idle != null then "\(($idle / 60) | floor)m" else "ON" end), r: "val"}]
         elif $clb == "STOPPED" then [{t: "Colab ", r: "lbl"}, {t: "OFF", r: "lbl"}]
         elif $clb == "STALE_OFFLINE" then [{t: "Colab!", r: "acc"}]
         elif $clb == "DEGRADED" then [{t: (if $err_kind == "rate_limit" then "Colab LIM" else "Colab ERR" end), r: "acc"}]
         else [{t: "Colab \($clb)", r: "acc"}] end) +
        (if ($gce // 0) > 0 then [{t: " G\($gce)", r: "acc"}] else [] end)
       else [] end) +
      (if $m_mbox and ($mt != null) and ($clb != "NOT_CONFIGURED") then
        (if (($m_sess and ($pc != null)) or ($m_colab and ($clb != "") and ($clb != "NOT_CONFIGURED"))) then [{t: "·", r: "lbl"}] else [] end) +
        (if $m_stale and ($mt == 0) and ($mf == 0) and ($md == 0) then [{t: "Q!", r: "acc"}]
         elif ($mf // 0) > 0 or $mu > 0 then [{t: "Q\($mt) ", r: "lbl"}, {t: "\(if ($mf // 0) > 0 then "\($mf)F" else "" end)\(if $mu > 0 then "\($mu)U" else "" end)", r: "acc"}]
         else [{t: "Q", r: "lbl"}, {t: "\($mt)", r: "val"}] end)
       else [] end) +
      (if $m_fresh and ($max_age >= 120 or (($cloud_ok | not) and ($clb == "DEGRADED"))) then
        [{t: "·", r: "lbl"}, {t: (
          if $max_age >= 999999 then "no-cache"
          elif ($cloud_ok | not) and ($clb == "DEGRADED") then (if $err_kind == "rate_limit" then "rc" else "deg" end)
          elif $max_age < 3600 then "\(($max_age / 60) | floor)m"
          else "\(($max_age / 3600) | floor)h" end
        ), r: "lbl"}]
       else [] end)
    ) as $p3_parts |
    (
      if (($mf // 0) > 0 or ($gce // 0) > 0 or $clb == "STALE_OFFLINE") then ["▲", "alert", true]
      elif ($clb == "DEGRADED" or $mu > 0 or $m_stale) then ["▲", "warn", true]
      elif $clb == "ONLINE" then ["●", "ok", false]
      else ["·", "base", false] end
    ) as $p3_meta |
    (if ($p3_parts | length) > 0 then . + [{icon: $p3_meta[0], parts: $p3_parts, slot: $p3_meta[1], tint: $p3_meta[2]}] else . end)
'

JQ_MAIN='
  [] |
  # 1. Context capsule (agy-capsule overload thresholds: >=75 warn, >=90 alert)
  if $m_ctx then
    if $ctx != null then
      (($ctx | round)) as $cp |
      (if $cp >= 90 then "alert" elif $cp >= 75 then "warn" else "ctx" end) as $ctx_slot |
      (if ($ind == "bar") or $wide_rich then
        [{t: (if $wide_rich then (if $lang == "en" then "Context " else "上下文 " end) elif $lang == "en" then "Ctx " else "上下文 " end), r: "lbl"}, {t: "\($cp)% ", r: "val"}, {t: bar6($cp), r: "acc"}]
       else
        [{t: (if $lang == "en" then "Ctx " else "上下文 " end), r: "lbl"}, {t: "\($cp)%", r: "val"}]
       end) as $c_parts |
      . + [{icon: (if $ind == "ring" then ring($cp) else "" end), parts: $c_parts, slot: $ctx_slot, tint: ($cp >= 75)}]
    else
      . + [{icon: "◈", parts: [{t: "Faiz", r: "val"}], slot: "ctx", tint: false}]
    end
  else . end |

  # 2. Prompt Cache Hit & TTL Probe capsule (3m warn / 4m cold alert heuristic)
  if $m_cache and ($cache_hit != null) then
    (
      if ($cache_age != null and $cache_age >= 240) then
        ["alert", (if $lang == "en" then " cold" else " 冷却" end), true]
      elif ($cache_age != null and $cache_age >= 180) then
        ["warn", " 3m!", true]
      elif $cache_hit < 50 then
        ["warn", "", true]
      else
        ["ok", "", false]
      end
    ) as $cmeta |
    . + [{
      icon: (if $ind == "ring" then ring($cache_hit) else "●" end),
      parts: (
        [{t: (if $lang == "en" then "Cache " else "缓存 " end), r: "lbl"}, {t: "\(pct_str($cache_hit; $short_pct))%", r: "val"}] +
        (if $cmeta[1] != "" then [{t: $cmeta[1], r: "acc"}]
         elif $wide_rich and ($cache_age != null) then [{t: (if $lang == "en" then " · Live" else " · 实时" end), r: "lbl"}]
         else [] end)
      ),
      slot: $cmeta[0],
      tint: $cmeta[2]
    }]
  else . end |

  # 3. Live Token Rate capsule (<10 alert, 10..24 warn, >=25 ok)
  if $m_speed and ($live_tps != null) and ($live_tps > 0) then
    (if $live_tps < 10 then ["alert", true] elif $live_tps < 25 then ["warn", true] else ["ok", false] end) as $smeta |
    . + [{
      icon: "▸",
      parts: (if $wide_rich then
                [{t: (if $lang == "en" then "Speed " else "速率 " end), r: "lbl"}, {t: "\($live_tps) tok/s", r: "val"}]
              elif $use_adaptive_2row then
                [{t: (if $lang == "en" then "Speed " else "速率 " end), r: "lbl"}, {t: "\($live_tps)t/s", r: "val"}]
              else
                [{t: "\($live_tps)", r: "val"}, {t: "t/s", r: "lbl"}]
              end),
      slot: $smeta[0],
      tint: $smeta[1]
    }]
  else . end |

  # 4. Quota & Argon capsules
  if $m_quota then
    if $pro5 != null or $prowk != null or $arg != null then
      ($use_adaptive_2row and ($eff_cols < 155) and ($q_prec != "int") and ($tight_quota | not)) as $compact_gem_lbl |
      (if ($cla_tight and $cla_show and $q_prec != "int") then "1dp" else $q_prec end) as $gem_prec |
      (
        if $compact_gem_lbl then
          (if $pro5 != null then
            [{t: "Gemini ", r: "lbl"}, {t: "\(quota_str($pro5; $gem_prec; $tight_quota))%", r: "val"}]
           else [{t: "Gemini ", r: "lbl"}, {t: "--", r: "val"}] end) +
          (if $prowk != null then [{t: "·", r: "lbl"}, {t: "\(quota_str($prowk; $gem_prec; $tight_quota))%", r: "val"}] else [{t: "·--", r: "lbl"}] end)
        else
          (if $pro5 != null then
            [{t: "Gemini ", r: "lbl"}, {t: "5h:\(quota_str($pro5; $q_prec; ($short_pct and $q_prec == "int")))%", r: "val"}] +
            (if $ind == "bar" then [{t: " \(bar6($pro5))", r: "acc"}] else [] end)
           else [{t: "Gemini ", r: "lbl"}, {t: "5h:--", r: "val"}] end) +
          (if $prowk != null then [{t: " · Wk:", r: "lbl"}, {t: "\(quota_str($prowk; $q_prec; ($short_pct and $q_prec == "int")))%", r: "val"}] else [{t: " · Wk:--", r: "lbl"}] end) +
          (if $wide_rich and ($reset_ep != null) and ($reset_ep > 0) then
            (($reset_ep - $now_ts) as $grem |
             if $grem > 0 then
               (if $grem >= 3600 then
                  (($grem / 3600) | floor) as $grh |
                  ((($grem % 3600) / 60) | floor) as $grm |
                  "\($grh)h\(if $grm < 10 then "0" else "" end)\($grm)m"
                else "\(($grem / 60) | floor)m" end) as $grtxt |
               [{t: " · ↻\($grtxt)", r: "lbl"}]
             else [] end)
           else [] end)
        end
      ) as $g_parts |
      (if ($pro5 != null and $pro5 < 20) then "alert"
       elif ($pro5 != null and $pro5 < 40) then "warn"
       else "base" end) as $g_slot |
      . + [{
        icon: (if $ind == "ring" and $pro5 != null then ring($pro5) else "" end),
        parts: $g_parts,
        slot: $g_slot,
        tint: ($g_slot != "base")
      }] |
      (
        (if $arg != null then
          (if ($cla_tight and $cla_show and $q_prec != "int") then
            [{t: "Arg ", r: "lbl"}, {t: "\($arg | fmt1)", r: "val"}]
           else
            [{t: (if ($use_adaptive_2row and $cla_show and ($wide_rich | not)) then "Arg " else "Argon " end), r: "lbl"}, {t: "\(quota_str($arg; $q_prec; ($short_pct and $q_prec == "int")))%", r: "val"}] +
            (if $ind == "bar" then [{t: " \(bar6($arg))", r: "acc"}] else [] end)
           end)
         else [{t: (if ($use_adaptive_2row and $cla_show and ($wide_rich | not)) then "Arg " else "Argon " end), r: "lbl"}, {t: "--", r: "val"}] end) +
        (if $gate == "WARN_PRO" then [{t: (if $lang == "en" then (if ($compact_gem_lbl or ($use_adaptive_2row and $cla_show)) then "!" elif $use_adaptive_2row then " Pro!" else " (Pro!)" end) else " (Pro低)" end), r: "acc"}]
         elif $gate == "THROTTLE_ARGON" then [{t: (if $lang == "en" then (if ($compact_gem_lbl or ($use_adaptive_2row and $cla_show)) then "!" elif $use_adaptive_2row then " Low" else " (Low)" end) else " (限)" end), r: "acc"}]
         else [] end) +
        (if ($use_adaptive_2row and $cla_show) then [{t: (if $wide_rich then " · Claude " else "·Cla " end), r: "lbl"}, {t: $cla_val, r: "val"}] else [] end)
      ) as $a_parts |
      (if $gate == "WARN_PRO" or ($use_adaptive_2row and $cla_show and $cla_min != null and $cla_min < 20) then "alert"
       elif ($arg != null and $arg < 20) or ($gate == "THROTTLE_ARGON") or ($use_adaptive_2row and $cla_show and $cla_min != null and $cla_min < 40) then "warn"
       else "base" end) as $a_slot |
      . + [{
        icon: (if $a_slot != "base" then "▲" elif $ind == "ring" and $arg != null then ring($arg) else "" end),
        parts: $a_parts,
        slot: $a_slot,
        tint: ($a_slot != "base")
      }] |
      (if ($cla_show and ($use_adaptive_2row | not)) then
        (if ($cla_min != null and $cla_min < 20) then "alert" elif ($cla_min != null and $cla_min < 40) then "warn" else "base" end) as $cl_slot |
        . + [{
          icon: (if $ind == "ring" and $cla_min != null then ring($cla_min) else "" end),
          parts: [{t: "Claude ", r: "lbl"}, {t: $cla_val, r: "val"}],
          slot: $cl_slot,
          tint: ($cl_slot != "base")
        }]
       else . end)
    else
      . + [{
        icon: "○",
        parts: [{t: (if $lang == "en" then "Quota syncing" else "配额同步中" end), r: "lbl"}],
        slot: "muted",
        tint: false
      }]
    end
  else . end |

  # 5. Secondary telemetry capsule (Session Token Burn | 5h Quota Reset | CLI Sessions)
  if $m_sess then
    if $sec_metric == "burn" and (($sess_tok != null and $sess_tok > 0) or ($tok1h != null)) then
      (
        if ($sess_tok != null and $sess_tok > 0) then
          if $sess_tok >= 1000000 then "\(($sess_tok / 1000000) | fmt2)M"
          elif $sess_tok >= 1000 then "\(($sess_tok / 1000) | fmt1)K"
          else "\($sess_tok)" end
        else
          if $tok1h >= 1000000 then "\(($tok1h / 1000000) | fmt1)M/h"
          elif $tok1h >= 1000 then "\(($tok1h / 1000) | round)K/h"
          elif $tok1h > 0 then "\($tok1h)/h"
          else "0K/h" end
        end
      ) as $burn_txt |
      (
        if $wide_rich and ($sess_tok != null and $sess_tok > 0) and ($tok1h != null and $tok1h > 0) then
          (if $tok1h >= 1000000 then "\(($tok1h / 1000000) | fmt1)M"
           elif $tok1h >= 1000 then "\(($tok1h / 1000) | round)K"
           else "\($tok1h)" end) as $h1_txt |
          [{t: (if $lang == "en" then "Session " else "本话 " end), r: "lbl"}, {t: $burn_txt, r: "val"}, {t: " · 1h:\($h1_txt)", r: "lbl"}]
        elif $wide_rich and ($sess_tok != null and $sess_tok > 0) then
          [{t: (if $lang == "en" then "Session " else "本话 " end), r: "lbl"}, {t: $burn_txt, r: "val"}]
        else
          [{t: (if $lang == "en" then "Burn " else "消耗 " end), r: "lbl"}, {t: $burn_txt, r: "val"}]
        end
      ) as $b_parts |
      . + [{
        icon: "◆",
        parts: $b_parts,
        slot: "base",
        tint: false
      }]
    elif $sec_metric == "reset" and ($reset_ep != null) then
      (
        if $reset_ep == 0 then (if $lang == "en" then "Full" else "满额" end)
        else
          ($reset_ep - $now_ts) as $rem |
          if $rem <= 0 then (if $lang == "en" then "Soon" else "重刷中" end)
          elif $rem >= 3600 then
            (($rem / 3600) | floor) as $rh |
            ((($rem % 3600) / 60) | floor) as $rm |
            "\($rh)h\(if $rm < 10 then "0" else "" end)\($rm)m"
          else "\(($rem / 60) | floor)m" end
        end
      ) as $rst_txt |
      . + [{
        icon: "↻",
        parts: [
          {t: (if $lang == "en" then "Reset " else "重置 " end), r: "lbl"},
          {t: $rst_txt, r: "val"}
        ],
        slot: "base",
        tint: false
      }]
    elif ($pc != null) then
      . + [{
        icon: "·",
        parts: [
          {t: (if $lang == "en" then "Sess " else "会话 " end), r: "lbl"},
          {t: (if $use_adaptive_2row then "\($pc) · \($gc // 0)" else "\($pc)·\($gc // 0)" end), r: "val"}
        ],
        slot: "base",
        tint: false
      }]
    else . end
  else . end |

  # 6. Colab & GCE capsule
  if $m_colab and ($clb != "") and ($clb != "NOT_CONFIGURED") then
    (
      if $clb == "ONLINE" then
        ["●", [{t: "Colab ", r: "lbl"}, {t: (if $wide_rich then ((if $lang == "en" then "ONLINE" else "在线" end) + (if $idle != null then (if $lang == "en" then " · \(($idle / 60) | floor)m left" else " · 余\(($idle / 60) | floor)分" end) else "" end)) else ((if $lang == "en" then "ON" else "在线" end) + (if $idle != null then "(\(($idle / 60) | floor)m)" else "" end)) end), r: "val"}], "ok", false]
      elif $clb == "STOPPED" then
        ["○", [{t: "Colab ", r: "lbl"}, {t: (if $lang == "en" then "IDLE" else "停机" end), r: "lbl"}], "muted", false]
      elif $clb == "STALE_OFFLINE" then
        ["▲", [{t: "Colab ", r: "lbl"}, {t: (if $lang == "en" then "STALE!" else "失联!" end), r: "acc"}], "alert", true]
      elif $clb == "DEGRADED" then
        (if $err_kind == "rate_limit" then
          ["▲", [{t: "Colab ", r: "lbl"}, {t: (if $wide_rich then (if $lang == "en" then "LIMIT · Rate" else "限流 · Rate" end) elif $lang == "en" then "LIMIT" else "限流" end), r: "acc"}], "warn", true]
         else
          ["▲", [{t: "Colab ", r: "lbl"}, {t: (if $wide_rich then (if $lang == "en" then "TIMEOUT · Net" else "超时 · Net" end) elif $lang == "en" then "TIMEOUT" else "超时" end), r: "acc"}], "warn", true]
         end)
      else
        ["◐", [{t: "Colab ", r: "lbl"}, {t: $clb, r: "acc"}], "warn", true]
      end
    ) as $cm |
    (if ($gce // 0) > 0 then [{t: (if $lang == "en" then " · GCE:\($gce)" else " · GCE:\($gce)台" end), r: "acc"}] else [] end) as $gce_p |
    (if ($gce // 0) > 0 then "alert" else $cm[2] end) as $c_slot |
    . + [{icon: $cm[0], parts: ($cm[1] + $gce_p), slot: $c_slot, tint: ($cm[3] or (($gce // 0) > 0))}]
  else . end |

  # 7. Mailbox Queue capsule
  if $m_mbox and ($mt != null) and ($clb != "NOT_CONFIGURED") then
    if $m_stale and ($mt == 0) and ($mf == 0) and ($md == 0) then
      . + [{
        icon: "▲",
        parts: [{t: (if $lang == "en" then "Queue " else "队列 " end), r: "lbl"}, {t: (if $lang == "en" then "LIMIT" else "限流" end), r: "acc"}],
        slot: "warn",
        tint: true
      }]
    elif $use_adaptive_2row then
      (
        if ($mf // 0) > 0 and $mt > 0 then
          ["▲", "alert", true, (if $wide_rich then (if $lang == "en" then "\($mt) Run · \($mf) Fail" else "\($mt)运行 · \($mf)失败" end) elif $lang == "en" then "\($mt)R · \($mf)F" else "\($mt)运 · \($mf)败" end)]
        elif ($mf // 0) > 0 then
          ["▲", "alert", true, (if $lang == "en" then "\($mf) Fail" else "\($mf)失败" end)]
        elif $mt > 0 then
          ["▸", "ok", false, (if $lang == "en" then "\($mt) Run" else "\($mt)运行" end)]
        else
          ["·", "base", false, (if $lang == "en" then "Idle" else "空闲" end)]
        end
      ) as $qm2 |
      (if $wide_rich and ($md > 0) then [{t: (if $lang == "en" then " · \($md) Done" else " · \($md)完成" end), r: "lbl"}]
       elif $m_fresh and ($max_age >= 180) and ($max_age < 999999) and ((($cloud_ok | not) and ($clb == "DEGRADED")) | not)
       then [{t: (if $lang == "en" then " · \(($max_age / 60) | floor)m" else " · \(($max_age / 60) | floor)分" end), r: "lbl"}]
       else [] end) as $inline_age |
      . + [{
        icon: $qm2[0],
        parts: ([
          {t: (if $lang == "en" then "Queue " else "队列 " end), r: "lbl"},
          {t: $qm2[3], r: (if $qm2[2] then "acc" else "val" end)}
        ] + $inline_age),
        slot: $qm2[1],
        tint: $qm2[2]
      }]
    else
      (if ($mf // 0) > 0 then ["alert", true] elif $mu > 0 then ["warn", true] else ["base", false] end) as $qm |
      (if $lang == "en" then "UNV" else "未核" end) as $unv_tag |
      . + [{
        icon: (if $qm[1] then "▲" else "" end),
        parts: [
          {t: (if $lang == "en" then "Queue " else "队列 " end), r: "lbl"},
          {t: (if $lang == "en" then "\($mt)Q·\($mf // 0)F" + (if $mu > 0 then "·\($mu)\($unv_tag)" else "" end)
               else "\($mt)待·\($mf // 0)败" + (if $mu > 0 then "·\($mu)\($unv_tag)" else "" end) end), r: (if $qm[1] then "acc" else "val" end)}
        ],
        slot: $qm[0],
        tint: $qm[1]
      }]
    end
  else . end |

  # 8. Freshness capsule (suppressed in adaptive <155c narrow mode so Row 2 never spills onto an orphan 3rd row)
  if $m_fresh and ($use_adaptive_2row | not) then
    (
      if $max_age >= 999999 then (if $lang == "en" then "no-cache" else "无缓存" end)
      elif ($cloud_ok | not) and ($clb == "DEGRADED") then
        (if $err_kind == "rate_limit" then
          (if $lang == "en" then "rc-limit" else "rc限流" end)
         else
          (if $lang == "en" then "degraded" else "云降级" end)
         end)
      elif $max_age < 90 then (if $lang == "en" then "live" else "实时" end)
      elif $max_age < 3600 then "\(($max_age / 60) | floor)m" + (if $lang == "en" then " ago" else "前" end)
      else "\(($max_age / 3600) | floor)h" + (if $lang == "en" then " ago" else "前" end)
      end
    ) as $ft |
    . + [{icon: "", parts: [{t: $ft, r: "lbl"}], slot: "muted", tint: false}]
  else . end
'

JQ_TAIL=' |
  (if ($eff_style == "minimal") then " | " elif $wide_rich then "  " elif ($use_compact or $use_adaptive_2row) then " " else "  " end) as $sep |
  ($sep | disp_len) as $sep_w |
  (if $use_adaptive_2row then pack_matrix_rows(.; $eff_cols; $auto_wrap; $sep_w; $eff_style; $wide_rich)
   else pack_rows(.; $eff_cols; $auto_wrap; $sep_w; $eff_style) end) as $rows |
  ($use_adaptive_2row and (($rows | length) == 2)) as $is_matrix |
  [
    range(0; ($rows | length)) as $ri |
    ($rows[$ri]) as $row |
    (if $is_matrix and ($color_mode == "row") then
      row_token_to_slot($row_colors[0])
     elif $color_mode == "row" and (($use_compact | not) or ($rows | length) > 1) then
      row_token_to_slot($row_colors[($ri % ($row_colors | length))])
     else "" end) as $row_slot |
    (
      [
        $row[] as $it |
        (if $row_slot != "" then $row_slot else $it.slot end) as $use_slot |
        slot_style($pal; $use_slot; $c16) as $sp |
        slot_style($pal; $it.slot; $c16) as $acc_sp |
        render_capsule($it; $sp; $acc_sp; $pal; $eff_style; $has_tc; ($it.tint and ($is_matrix or $color_mode == "semantic")))
      ] | join($sep)
    )
  ] as $rendered_rows |
  (
    if $is_matrix then
      ([ $rows[0][] | cap_width(.; $eff_style) ] | add) + ((($rows[0] | length) - 1) * $sep_w)
    else 0 end
  ) as $grid_w |
  (if $has_tc then "\u001b[38;2;54;59;69m" + repeat_str("─"; $grid_w) + rst else repeat_str("-"; $grid_w) end) as $top_rule |
  (if $has_tc then "\u001b[38;2;54;59;69m" + repeat_str("╌"; $grid_w) + rst else repeat_str("-"; $grid_w) end) as $mid_rule |
  (if $is_matrix and $line_gap then "\n\($mid_rule)\n"
   elif $line_gap and (($rendered_rows | length) > 1) and (($rendered_rows | length) <= 3) then "\n\n"
   else "\n" end) as $row_join |
  ($rendered_rows | join($row_join)) as $body |
  if $is_matrix and $top_gap and ($body != "") then "\($top_rule)\n\($body)"
  elif $top_gap and (($rendered_rows | length) == 1) and ($body != "") then "\n\($body)"
  else $body end
'

case "$cfg_raw" in
    *'"compact"'*) JQ_BODY="$JQ_COMPACT" ;;
    *) JQ_BODY="$JQ_MAIN" ;;
esac
JQ_FILTER="${JQ_FILTER}${JQ_BODY}${JQ_TAIL}"
unset JQ_COMPACT JQ_MAIN JQ_BODY JQ_TAIL

if [ -n "$t_path" ] && [ -s "$t_path" ]; then
    if [ "$t_sz" -le 32768 ] 2>/dev/null; then
        exec "$JQ_BIN" "${jq_args[@]}" --slurpfile raw_t "$t_path" "$JQ_FILTER"
    else
        "$JQ_BIN" "${jq_args[@]}" --slurpfile raw_t <(tail -n 40 "$t_path" 2>/dev/null) "$JQ_FILTER"
    fi
else
    exec "$JQ_BIN" "${jq_args[@]}" --argjson raw_t '[]' "$JQ_FILTER"
fi
