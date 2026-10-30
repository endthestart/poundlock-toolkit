#!/bin/sh
# fetch-pig.sh VERSION SHA256: download PiG's Linux x86_64 release archive into recipes/pig/,
# check it against the SHA-256 the release publishes, and keep only the pig binary.
# Nothing runs from the archive here; recipes/PiG.Dockerfile copies the binary into an image.
set -eu
version=$1 expected=$2
case "$version" in *[!0-9.]*|'') echo "usage: fetch-pig.sh VERSION SHA256" >&2; exit 2 ;; esac
case "$expected" in *[!0-9a-f]*|'') echo "SHA256 must be 64 lowercase hex digits" >&2; exit 2 ;; esac
[ "${#expected}" -eq 64 ] || { echo "SHA256 must be 64 lowercase hex digits" >&2; exit 2; }
dir=$(dirname "$0")/pig
archive="pig-$version-linux-amd64.tar.gz"
base="https://github.com/MichaelKinsy/PiG/releases/download/v$version"
mkdir -p "$dir"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL --proto '=https' -o "$work/$archive" "$base/$archive"
curl -fsSL --proto '=https' -o "$work/SHA256SUMS" "$base/SHA256SUMS"
actual=$(shasum -a 256 "$work/$archive" | cut -d' ' -f1)
[ "$actual" = "$expected" ] || { echo "$archive is $actual, not $expected" >&2; exit 1; }
grep -q "^$expected  $archive\$" "$work/SHA256SUMS" \
  || { echo "the release's SHA256SUMS does not list $expected for $archive" >&2; exit 1; }
tar -xzf "$work/$archive" -C "$work"
binary=$(find "$work" -type f -name pig -perm -u+x | head -1)
[ -n "$binary" ] || { echo "no pig binary in $archive" >&2; exit 1; }
cp "$binary" "$dir/pig"
echo "recipes/pig/pig: PiG $version, $archive $expected"
