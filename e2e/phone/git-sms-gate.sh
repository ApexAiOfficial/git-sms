#!/data/data/com.termux/files/usr/bin/bash
# git-sms-gate: the only program a Git SMS SSH key may run on an Android/Termux handset.
#
# Installed as the forced command of one key in ~/.ssh/authorized_keys:
#   restrict,command="/data/data/com.termux/files/home/bin/git-sms-gate" ssh-ed25519 AAAA... git-sms-gate
#
# It can only text numbers listed in ~/.config/git-sms-gate/allow (one E.164 number per
# line), and can only return the conversation with those numbers, filtered on the phone,
# so nothing else in the handset's SMS history ever leaves it. No shell, no other command.
# (Termux:API's -f sender filter returns both directions of that conversation; each message
# carries "type": "inbox" or "sent", and callers must keep only "inbox".)
#
#   ping                    Termux:API presence and permission state (no message data)
#   send +15550100001 B64   text the base64-encoded body to an allowed number
#   inbox +15550100001 [N]  the newest N (default 20, at most 50) messages exchanged with an allowed number, as JSON
set -euo pipefail
export PATH=/data/data/com.termux/files/usr/bin:$PATH
ALLOW="$HOME/.config/git-sms-gate/allow"
cmd="${SSH_ORIGINAL_COMMAND:-}"
E164='\+[1-9][0-9]{6,14}'

refuse() { echo "refused${1:+: $1}"; exit 2; }
allowed() { [[ -f "$ALLOW" ]] && grep -qxF -- "$1" "$ALLOW"; }
has_sender_filter() { # capture first: piping into grep -q can SIGPIPE the writer and fail under pipefail
  local help
  help=$(termux-sms-list -h 2>&1 || true)
  [[ "$help" == *"-f"* ]]
}
probe() { # prints ok or denied for one Termux:API call, never its data
  local out
  out=$(timeout 25 "$@" 2>&1 || true)
  if [[ "$out" == *"grant the following permission"* ]]; then echo denied
  elif [[ -z "$out" ]]; then echo "no-response"
  else echo ok; fi
}

if [[ "$cmd" == "ping" ]]; then
  echo "gate: git-sms-gate v4"
  echo "termux-api: $(command -v termux-sms-send >/dev/null && echo present || echo missing)"
  echo "allowlist: $(grep -c . "$ALLOW" 2>/dev/null || echo 0) numbers"
  echo "sms_read: $(probe termux-sms-list -l 1 -t inbox)"
  echo "telephony: $(probe termux-telephony-deviceinfo)"
  if has_sender_filter; then echo "sender_filter: supported"; else echo "sender_filter: missing"; fi
  exit 0
fi

# Keep every regex repeat bound at or below 255: Android's bionic libc rejects larger ones
# (RE_DUP_MAX), the match then silently fails, and the request falls through to "refused".
if [[ "$cmd" =~ ^send\ ($E164)\ ([A-Za-z0-9+/=]+)$ ]]; then
  number="${BASH_REMATCH[1]}"
  encoded="${BASH_REMATCH[2]}"
  (( ${#encoded} >= 4 && ${#encoded} <= 2200 )) || refuse "body length"
  allowed "$number" || refuse "number not allowed"
  body=$(printf '%s' "$encoded" | base64 -d 2>/dev/null) || refuse "bad body"
  (( ${#body} >= 1 && ${#body} <= 1600 )) || refuse "body length"
  out=$(timeout 40 termux-sms-send -n "$number" "$body" 2>&1) || refuse "send failed"
  [[ -z "$out" ]] && echo sent || { echo "send error: ${out:0:200}"; exit 3; }
  exit 0
fi

if [[ "$cmd" =~ ^inbox\ ($E164)(\ ([0-9]{1,2}))?$ ]]; then
  number="${BASH_REMATCH[1]}"
  limit="${BASH_REMATCH[3]:-20}"
  (( limit >= 1 && limit <= 50 )) || refuse "limit"
  allowed "$number" || refuse "number not allowed"
  has_sender_filter || refuse "termux-sms-list has no sender filter"
  out=$(timeout 40 termux-sms-list -t inbox -d -n -l "$limit" -f "$number" 2>&1) || refuse "list failed"
  if [[ "$out" =~ ^\[[[:space:]]*\]$ && "$number" == +1* ]]; then
    # Some carriers store North American senders without the +1.
    out=$(timeout 40 termux-sms-list -t inbox -d -n -l "$limit" -f "${number#+1}" 2>&1) || refuse "list failed"
  fi
  printf '%s\n' "$out"
  exit 0
fi

refuse
