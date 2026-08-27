#!/usr/bin/env bash
# Mechanical transcript recorder. Bytes in the output file come from the
# process, never from a human/agent retyping them.
#
# Usage:
#   _rec.sh <outfile> "<step id>" "<VERDICT>" "<note>" "<runbook-cmd or ''>" "<actual cmd>"
GIST="$1"; STEP="$2"; VERDICT="$3"; NOTE="$4"; RBCMD="$5"; CMD="$6"
{
  printf '\n## %s\n\n' "$STEP"
  if [ -n "$NOTE" ]; then printf '%s\n\n' "$NOTE"; fi
  if [ -n "$RBCMD" ]; then
    printf 'Command as the runbook writes it:\n\n```bash\n%s\n```\n\n' "$RBCMD"
    printf 'Command actually run:\n\n```bash\n%s\n```\n\n' "$CMD"
  else
    printf 'Command:\n\n```bash\n%s\n```\n\n' "$CMD"
  fi
  printf 'Output (captured, verbatim):\n\n```\n'
} >> "$GIST"
OUTF=$(mktemp)
bash -c "$CMD" > "$OUTF" 2>&1
RC=$?
cat "$OUTF" >> "$GIST"
{
  printf '```\n\n'
  printf '`exit status: %s`\n\n' "$RC"
  printf 'Verdict: **%s**\n' "$VERDICT"
} >> "$GIST"
cat "$OUTF"
echo "___RC=$RC BYTES=$(wc -c < "$OUTF")___"
rm -f "$OUTF"
