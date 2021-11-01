#!/usr/bin/env bash
set -e

while read -r media_path; do
    text_path="${media_path%.*}.txt"
    if [ ! -s "${text_path}" ]; then
        echo "Missing ${text_path}"
        continue
    fi

    text="$(cat "${text_path}")"

    sync_path="${media_path%.*}.json"
    if [ -s "${sync_path}" ]; then
        # Already exists
        continue
    fi

    duration="$(ffprobe -v error -select_streams a:0 -show_entries stream=duration -of default=noprint_wrappers=1:nokey=1 "${media_path}")"
    jq --null-input \
        --argjson s '0.0' --argjson e "${duration}" --arg t "${text}" \
        '{ start:$s, end:$e, text:$t }' \
        > "${sync_path}"

    echo "${sync_path}"
done
