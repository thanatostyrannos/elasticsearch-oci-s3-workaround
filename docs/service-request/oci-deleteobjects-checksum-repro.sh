#!/usr/bin/env bash
# Minimal reproduction: OCI S3 Compatibility rejects x-amz-checksum-crc32.
#
# Sends the SAME DeleteObjects request four times against the same bucket, with
# the same credentials, varying only the integrity header:
#
#     Content-MD5             expected 200
#     x-amz-checksum-crc32c   expected 200
#     x-amz-checksum-sha256   expected 200
#     x-amz-checksum-crc32    expected 400  <-- the defect
#
# Amazon S3 accepts all four. OCI accepts three and rejects crc32, whose only
# difference from crc32c is the polynomial.
#
# This is a line-for-line port of oci-deleteobjects-checksum-repro.py, for sites
# where the support engineer has a RHEL shell and no Python they are allowed to
# run. It prints the same report, so the two are diffable once the timestamp and
# the request id, which differ on every run by construction, are masked.
#
# Nothing outside a base RHEL install is used: bash, coreutils, openssl, curl,
# gzip. No jq, no python, no awscli.
#
# The keys named below do not exist. DeleteObjects on an absent key is a success
# on S3 and on OCI, so a run that reaches the store deletes nothing: the request
# is rejected before OCI looks at the keys at all.
#
#     ./oci-deleteobjects-checksum-repro.sh \
#         --endpoint https://<ns>.compat.objectstorage.<region>.oraclecloud.com \
#         --region <region> --bucket <bucket> --credentials creds.json

set -uo pipefail

# Byte ordering, not the operator's locale, decides how the canonical headers
# sort. SigV4 fails opaquely if a locale collates them any other way.
export LC_ALL=C

ALGORITHM="AWS4-HMAC-SHA256"

die() { printf '%s\n' "$*" >&2; exit 2; }

usage() {
    sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# --- primitives ------------------------------------------------------------

# Hex-encode stdin. od rather than xxd: xxd ships in vim-common, which a
# minimal RHEL install does not have.
str_to_hex() { od -An -v -tx1 | tr -d ' \n'; }

# HMAC-SHA256 of stdin under a hex key, printed as hex. The key is binary at
# every step of the SigV4 chain, so it cannot be passed as a string.
hmac_hex() { openssl dgst -sha256 -mac HMAC -macopt "hexkey:$1" | sed 's/.*= *//'; }

# Takes a file, or stdin when called with no argument: the payload digest is
# taken over a file, the canonical request digest over a pipe.
sha256_hex() { openssl dgst -sha256 ${1+"$1"} | sed 's/.*= *//'; }
sha256_b64() { openssl dgst -sha256 -binary "$1" | openssl base64 -A; }
md5_b64()    { openssl dgst -md5    -binary "$1" | openssl base64 -A; }

# A big-endian uint32, as base64. This is how S3 frames every x-amz-checksum-*.
hex32_to_b64() {
    local h=$1
    printf "\\x${h:0:2}\\x${h:2:2}\\x${h:4:2}\\x${h:6:2}" | openssl base64 -A
}

# CRC-32, the IEEE polynomial. gzip already computes it and stores it in the
# last 8 bytes of its own trailer, little-endian, ahead of the input size.
# Reading it back is exact and costs one pipe; reimplementing it is neither.
crc32_hex() {
    local le
    le=$(gzip -c "$1" | tail -c 8 | head -c 4 | str_to_hex)
    printf '%s%s%s%s' "${le:6:2}" "${le:4:2}" "${le:2:2}" "${le:0:2}"
}

CRC32C_POLY=$(( 0x82F63B78 ))

# CRC-32C, the Castagnoli polynomial. No RHEL base tool computes it, so it is
# done here bit by bit. The body is a couple of hundred bytes; the cost is
# invisible and the alternative is a dependency.
crc32c_hex() {
    local crc=$(( 0xFFFFFFFF )) byte i
    while read -r byte; do
        crc=$(( crc ^ 10#$byte ))
        for i in 1 2 3 4 5 6 7 8; do
            if (( crc & 1 )); then
                crc=$(( (crc >> 1) ^ CRC32C_POLY ))
            else
                crc=$(( crc >> 1 ))
            fi
        done
    done < <(od -An -v -tu1 -w1 "$1")
    printf '%08x' $(( crc ^ 0xFFFFFFFF ))
}

# --- SigV4, inlined so this file runs anywhere with nothing installed -------

# Args: access_key secret_key method canonical_uri canonical_query
#       payload_sha256 region service amz_date  <header lines on stdin>
# Each stdin line is an already-lowercased "name:value".
authorization() {
    local access_key=$1 secret_key=$2 method=$3 canonical_uri=$4 \
          canonical_query=$5 payload_sha256=$6 region=$7 service=$8 amz_date=$9
    local sorted signed canonical_headers canonical_request datestamp scope \
          to_sign k0 k1 k2 k3 k4 signature

    sorted=$(sort)
    canonical_headers=$(printf '%s\n' "$sorted")
    signed=$(printf '%s\n' "$sorted" | cut -d: -f1 | paste -sd ';' -)

    canonical_request=$(printf '%s\n%s\n%s\n%s\n\n%s\n%s' \
        "$method" "$canonical_uri" "$canonical_query" \
        "$canonical_headers" "$signed" "$payload_sha256")

    datestamp=${amz_date:0:8}
    scope="$datestamp/$region/$service/aws4_request"
    to_sign=$(printf '%s\n%s\n%s\n%s' "$ALGORITHM" "$amz_date" "$scope" \
        "$(printf '%s' "$canonical_request" | sha256_hex)")

    k0=$(printf '%s' "AWS4$secret_key" | str_to_hex)
    k1=$(printf '%s' "$datestamp"    | hmac_hex "$k0")
    k2=$(printf '%s' "$region"       | hmac_hex "$k1")
    k3=$(printf '%s' "$service"      | hmac_hex "$k2")
    k4=$(printf '%s' "aws4_request"  | hmac_hex "$k3")
    signature=$(printf '%s' "$to_sign" | hmac_hex "$k4")

    printf '%s Credential=%s/%s, SignedHeaders=%s, Signature=%s' \
        "$ALGORITHM" "$access_key" "$scope" "$signed" "$signature"
}

# --- credentials -----------------------------------------------------------

# creds.json holds s3.access_key_id and s3.secret_access_key. jq is not in a
# base RHEL install, so the two scalars are lifted out with sed after the
# object is broken onto one field per line. Values are never echoed.
json_scalar() {
    tr ',{}' '\n\n\n' < "$1" \
        | sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" \
        | head -n 1
}

# --- main ------------------------------------------------------------------

endpoint="" region="" bucket="" credentials=""
while [ $# -gt 0 ]; do
    case "$1" in
        --endpoint)    endpoint=$2;    shift 2 ;;
        --region)      region=$2;      shift 2 ;;
        --bucket)      bucket=$2;      shift 2 ;;
        --credentials) credentials=$2; shift 2 ;;
        -h|--help)     usage 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
[ -n "$endpoint" ]    || die "--endpoint is required"
[ -n "$region" ]      || die "--region is required"
[ -n "$bucket" ]      || die "--bucket is required"
[ -n "$credentials" ] || die "--credentials is required"
[ -r "$credentials" ] || die "cannot read credentials file: $credentials"

access_key_id=$(json_scalar "$credentials" access_key_id)
secret_access_key=$(json_scalar "$credentials" secret_access_key)
[ -n "$access_key_id" ]     || die "no s3.access_key_id in $credentials"
[ -n "$secret_access_key" ] || die "no s3.secret_access_key in $credentials"

work=$(mktemp -d) || die "mktemp failed"
trap 'rm -rf "$work"' EXIT
body="$work/body.xml"
resp="$work/resp"
hdrs="$work/hdrs"

# One string, no trailing newline: the bytes signed must be the bytes sent.
printf '%s' '<?xml version="1.0" encoding="UTF-8"?><Delete xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Object><Key>does-not-exist/probe-a</Key></Object><Object><Key>does-not-exist/probe-b</Key></Object><Quiet>false</Quiet></Delete>' > "$body"

scheme=${endpoint%%://*}
host=${endpoint#*://}
host=${host%/}
canonical_uri="/$bucket"
canonical_query="delete="
payload_sha256=$(sha256_hex "$body")
body_len=$(wc -c < "$body")

printf 'endpoint : %s\n' "$endpoint"
printf 'bucket   : %s\n' "$bucket"
printf 'region   : %s\n' "$region"
printf 'body     : %s bytes, sha256 %s\n' "$body_len" "$payload_sha256"
printf '\n'

md5_value=$(md5_b64 "$body")
crc32c_value=$(hex32_to_b64 "$(crc32c_hex "$body")")
sha256_value=$(sha256_b64 "$body")
crc32_value=$(hex32_to_b64 "$(crc32_hex "$body")")

# Order matters only in that the report must read the same as the Python one.
for variant in \
    "Content-MD5|content-md5|$md5_value" \
    "x-amz-checksum-crc32c|x-amz-checksum-crc32c|$crc32c_value" \
    "x-amz-checksum-sha256|x-amz-checksum-sha256|$sha256_value" \
    "x-amz-checksum-crc32|x-amz-checksum-crc32|$crc32_value"
do
    label=${variant%%|*}
    rest=${variant#*|}
    header_name=${rest%%|*}
    header_value=${rest#*|}

    amz_date=$(date -u +%Y%m%dT%H%M%SZ)

    auth=$(printf '%s\n' \
        "host:$host" \
        "x-amz-content-sha256:$payload_sha256" \
        "x-amz-date:$amz_date" \
        "content-type:application/xml" \
        "$header_name:$header_value" \
        | authorization "$access_key_id" "$secret_access_key" POST \
              "$canonical_uri" "$canonical_query" "$payload_sha256" \
              "$region" s3 "$amz_date")

    url="$scheme://$host$canonical_uri?$canonical_query"
    sent_utc=$(date -u +%Y-%m-%dT%H:%M:%S.%6N+00:00)

    # -H 'Expect:' because curl would otherwise negotiate 100-continue, which
    # OCI answers in a way that splits the response across two header blocks.
    status=$(curl -sS -X POST "$url" \
        --data-binary "@$body" \
        -H "host: $host" \
        -H "x-amz-content-sha256: $payload_sha256" \
        -H "x-amz-date: $amz_date" \
        -H "content-type: application/xml" \
        -H "$header_name: $header_value" \
        -H "Authorization: $auth" \
        -H 'Expect:' \
        --max-time 30 \
        -D "$hdrs" -o "$resp" -w '%{http_code}' 2>"$work/curlerr")

    if [ -z "$status" ] || [ "$status" = "000" ]; then
        printf '%-24s %s %s\n' "$label" "000" "TRANSPORT-ERROR"
        printf '  sent (UTC)      : %s\n' "$sent_utc"
        printf '  opc-request-id  : \n'
        printf '  response        : %s\n' "$(tr -d '\r' < "$work/curlerr" | head -c 300)"
        printf '\n'
        continue
    fi

    request_id=$(sed -n 's/^[Oo][Pp][Cc]-[Rr]equest-[Ii][Dd]:[[:space:]]*//p' "$hdrs" \
        | tr -d '\r' | tail -n 1)
    if [ -z "$request_id" ]; then
        request_id=$(sed -n 's/^[Xx]-[Aa][Mm][Zz]-[Rr]equest-[Ii][Dd]:[[:space:]]*//p' "$hdrs" \
            | tr -d '\r' | tail -n 1)
    fi

    if [ "$status" = "200" ]; then
        verdict="ACCEPTED"
        detail=""
    else
        verdict="REJECTED"
        # Python reads 400 bytes, strips, then prints at most 300.
        detail=$(head -c 400 "$resp" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | head -c 300)
    fi

    printf '%-24s %s %s\n' "$label" "$status" "$verdict"
    printf '  sent (UTC)      : %s\n' "$sent_utc"
    printf '  opc-request-id  : %s\n' "$request_id"
    [ -n "$detail" ] && printf '  response        : %s\n' "$detail"
    printf '\n'
done
