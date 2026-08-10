#!/bin/bash
# The full CI pipeline
set -Eeuo pipefail
cd "$(dirname "$0")/.."
buck=tools/buck

# GitHub folds each step into a log group; an interactive terminal gets a bold banner; anything else
# (piped to a file, another CI) gets a bare `=== label ===` line with no escape sequences.
if [ "${GITHUB_ACTIONS:-}" = true ]; then mode=github
elif [ -t 1 ]; then
    mode=tty
    bold=$'\033[1m'
    dim=$'\033[2m'
    reset=$'\033[0m'
    # The rule's accent is an ANSI palette colour (not a fixed RGB), so the terminal keeps it legible
    # on its own light/dark theme. Not bold: that renders bright and washes out on light backgrounds.
    accent=''
    [ -z "${NO_COLOR:-}" ] && accent=$'\033[36m'
else mode=plain
fi

# A full-width ━ rule in the accent colour.
hrule() {
    local width fill
    width=$(tput cols 2>/dev/null || echo 72)
    printf -v fill '%*s' "$width" ''
    printf '%s%s%s\n' "$accent" "${fill// /━}" "$reset"
}

# A step banner: blank line; a rule opening `━━━ label ` with the label default-fg bold and the dashes
# accented; the dim command for copy-paste; then a closing rule.
banner() {
    local label=$1 width tail used
    shift
    width=$(tput cols 2>/dev/null || echo 72)
    used=$((4 + ${#label} + 1))  # "━━━ " + label + trailing space
    printf -v tail '%*s' "$((width > used ? width - used : 0))" ''
    printf '\n%s━━━ %s%s%s %s%s%s\n' "$accent" "$reset$bold" "$label" "$reset" "$accent" "${tail// /━}" "$reset"
    printf '%s%s%s\n' "$dim" "$*" "$reset"
    hrule
}

# group LABEL -- CMD...: run one labelled step, fail-fast.
#
# Never test the step's status (`"$@" || rc=$?`): bash then ignores errexit for it, and for every
# function it calls, so a step would run on past its own first failure and report the status of
# whatever ran last. The GitHub failure path goes through the ERR trap below instead, and `step` is
# global for the trap to name.
group() {
    step=$1
    shift 2
    case $mode in
        github) printf '::group::%s\n' "$step" ;;
        tty) banner "$step" "$@" ;;
        plain) printf '\n=== %s ===\n' "$step" ;;
    esac
    "$@"
    if [ "$mode" = github ]; then printf '::endgroup::\n'; fi
}

# On GitHub, close the group and surface an ::error:: after it (a failure buried inside a collapsed
# group is invisible). errtrace (-E above) carries this into the step functions.
if [ "$mode" = github ]; then
    trap 'printf "::endgroup::\n::error::ci step %s failed\n" "$step"' ERR
fi

# Build the from-source Rust image's SBOM to stdout and assert the crate graph reached it. Captured on
# its own line so `set -e` still catches a buck failure before grep runs.
rust_sbom() {
    local sbom
    sbom=$("$buck" build 'tine//examples/image-rust-project:demo[sbom][cyclonedx]' --out -)
    # our own rust packages
    grep -q 'pkg:cargo/hello@0.1.0' <<< "$sbom"
    grep -q 'pkg:cargo/nodeps@0.1.0' <<< "$sbom"
    # hello's dependency from crates.io
    grep -q 'pkg:cargo/serde_json@' <<< "$sbom"
    # hello's dependency from git
    grep -q 'pkg:cargo/anyhow@' <<< "$sbom"
}

# The same for the from-source Go image: the modules go embeds in the binary must reach the SBOM.
go_sbom() {
    local sbom
    sbom=$("$buck" build 'tine//examples/image-go-project:demo[sbom][cyclonedx]' --out -)
    # our own go modules, unversioned: -buildvcs=false leaves a main module's version at (devel)
    grep -q 'pkg:golang/example.com/hello' <<< "$sbom"
    grep -q 'pkg:golang/example.com/nodeps' <<< "$sbom"
    # hello's direct dependency
    grep -q 'pkg:golang/rsc.io/quote@' <<< "$sbom"
    # hello's indirect dependency
    grep -q 'pkg:golang/golang.org/x/text@' <<< "$sbom"
}

# The secure-boot image strips the package database from its partitions, so only the logical image
# still carries one for syft to read. Assert the rpm packages still reach its SBOM: without the
# database syft falls back to what it can guess from binaries, which reports a fraction of them.
secureboot_sbom() {
    local sbom
    sbom=$("$buck" build 'tine//examples/image-secureboot:image[sbom][cyclonedx]' --out -)
    grep -q 'pkg:rpm/fedora/systemd@' <<< "$sbom"
    grep -q 'pkg:rpm/fedora/kernel-core@' <<< "$sbom"
    # No binary to guess from: only the database reports these.
    grep -q 'pkg:rpm/fedora/fedora-release@' <<< "$sbom"
    grep -q 'pkg:rpm/fedora/filesystem@' <<< "$sbom"
}

# Assert the UKI's module selection still carries what the demo image boots through. The smokes below
# prove the same thing by booting, but this pins it against the real kernel without a VM, so a change to
# the default list that drops one of these fails here with the module named.
uki_modules() {
    local manifest
    manifest=$("$buck" build 'tine//examples/image:boot-demo.fedora[uki][modules]' --out -)
    # Only modules Fedora builds as modules: it links dm-mod, ext4, virtio_blk, virtio_pci, ahci and
    # sd_mod into the kernel, so no initrd carries those and the smokes cover them instead.
    local module
    for module in erofs dm-verity loop overlay nvme vfat virtio_scsi virtio_net virtiofs; do
        grep -q "\"path\": \".*/$module\.ko" <<< "$manifest" ||
            { echo "uki modules: $module is missing" >&2; return 1; }
    done
}

# What the demo disk boots, extracted out of its own ESP. The selection is semantic (the newest
# kernel, its UKI, the sections inside it), so it can go wrong without any target failing; assert by
# magic that each artifact is what it claims to be, and that the extracted initrd is the one the UKI
# carries rather than the initrd image the composition was handed.
boot_artifacts() {
    local scratch target=tine//examples/image:boot-demo.fedora
    scratch=$(mktemp -d)
    "$buck" build "$target[boot][kernel]" --out "$scratch/vmlinuz"
    "$buck" build "$target[boot][uki]" --out "$scratch/uki.efi"
    "$buck" build "$target[boot][initrd]" --out "$scratch/initrd"
    "$buck" build "$target[initrd]" --out "$scratch/image.cpio.zst"
    # A bzImage carries "HdrS" at 0x202 and a PE binary "MZ" at 0.
    test "$(dd if="$scratch/vmlinuz" bs=1 skip=514 count=4 status=none)" = HdrS
    test "$(dd if="$scratch/uki.efi" bs=1 count=2 status=none)" = MZ
    # The UKI appends the modules initrd to the one it was given, so the extracted one is larger.
    test "$(stat -c%s "$scratch/initrd")" -gt "$(stat -c%s "$scratch/image.cpio.zst")"
    rm -rf "$scratch"
}

# A DDI's file name is what a systemd-sysupdate transfer matches, so it is part of the contract
# rather than an implementation detail. The example sets no version, so this pins the rule's default
# alongside the shape.
sysext_name() {
    local output
    output=$("$buck" targets --show-output tine//examples/image:demo-ext.fedora | awk '{print $2}')
    test "$(basename "$output")" = demo-ext_0_x86-64.sysext.raw
}

# The release directory is where every published name meets. Assert the set the example publishes,
# that the verity pair is named after the two halves of the root hash (which is what lets
# systemd-sysupdate give the partitions it writes the UUIDs dissection pairs them by), and that the
# ESP is not among them.
release_artifacts() {
    local directory names roothash expected
    "$buck" build tine//examples/image-secureboot:release
    directory=$("$buck" targets --show-output tine//examples/image-secureboot:release | awk '{print $2}')
    names=$(ls "$directory")
    for expected in image_0_x86-64.raw image_0_x86-64.qcow2 image_0_x86-64.efi \
        image_0_x86-64.vmlinuz image_0_x86-64.initrd demo-ext_0_x86-64.sysext.raw; do
        grep -qx "$expected" <<< "$names" || { echo "release: $expected is missing" >&2; return 1; }
    done
    if grep -q esp <<< "$names"; then echo "release: the ESP must not be published" >&2; return 1; fi
    roothash=$("$buck" build 'tine//examples/image-secureboot:image[roothash]' --out -)
    grep -qx "image_0_x86-64.usr-x86-64.${roothash:0:32}.raw" <<< "$names"
    grep -qx "image_0_x86-64.usr-x86-64-verity.${roothash:32:32}.raw" <<< "$names"
}

# Sign the Secure Boot example through PKCS#11 tokens, exercising the external-key path end to end
# with the production module: tools/signing-server serves one tpm2-pkcs11 token per key from a
# software TPM, and the same example builds against its socket. Every tool this needs comes from the
# swtpm-signing box, so a runner needs nothing beyond what the rest of the pipeline uses. The
# externally signed image must also boot, which reuses the vm-smoke checks minus the two reading the
# certificates only generated-key builds carry.
# Stopping the arrangement is one TERM, per tools/signing-server. The log surfaces only on failure;
# the daemons keep spamming it during the build (swtpm logs every client disconnect), so it stays
# out of the console on success.
pkcs11_cleanup() {
    local status=$?
    if [ "$status" -ne 0 ] && [ -e "$pkcs11_dir/log" ]; then cat "$pkcs11_dir/log" >&2; fi
    # Already gone when the server itself failed, which the socket wait below then reports.
    if kill -0 "$server_pid" 2> /dev/null; then kill "$server_pid"; fi
    rm -rf "$pkcs11_dir"
}

secureboot_pkcs11() {
    # On a fresh runner this resolves and installs the box engine, so run it in the foreground
    # where that shows progress, rather than sitting silently in the backgrounded server's log.
    "$buck" build tine//box:swtpm-signing

    pkcs11_dir=$(mktemp -d)
    "$buck" run tine//tools:signing-server -- "$pkcs11_dir" > "$pkcs11_dir/log" 2>&1 &
    server_pid=$!
    trap pkcs11_cleanup EXIT
    until [ -S "$pkcs11_dir/sock/pkcs11" ]; do
        # Any setup failure exits the server before it gets to listen; its log then says why.
        kill -0 "$server_pid" 2> /dev/null || return 1
        sleep 0.5
    done

    # A prod build host materializes the signing coordinates in a file and points the build at it
    cat > "$pkcs11_dir/signing.bcfg" <<EOF
[signing]
token = SecureBoot
pcr-token = PcrPolicy
pin-file = $pkcs11_dir/pin
socket = $pkcs11_dir/sock/pkcs11
EOF
    local config=(--config-file "$pkcs11_dir/signing.bcfg")

    "$buck" build "${config[@]}" tine//examples/image-secureboot:image
    "$buck" build "${config[@]}" 'tine//examples/image-secureboot:image[uki]' --out "$pkcs11_dir/ukis"

    # The public key the UKI hands the booted system must be the PcrPolicy token's, and not the
    # Secure Boot one. Read each side into a variable first: a command substitution inside a `test`
    # argument is exempt from errexit, so a certificate that cannot be read would yield an empty
    # string and satisfy the inequality below without comparing anything.
    local box=("$buck" run tine//box:swtpm-signing --) uki=("$pkcs11_dir"/ukis/*.efi)
    local pcrpkey pcr_certificate secure_boot_certificate
    # ukify takes the file before the options, per `ukify inspect --help`.
    "${box[@]}" ukify inspect "${uki[0]}" --section ".pcrpkey:binary@$pkcs11_dir/pcrpkey"
    pcrpkey=$("${box[@]}" openssl pkey -pubin -in "$pkcs11_dir/pcrpkey" -pubout)
    pcr_certificate=$("${box[@]}" openssl x509 -in "$pkcs11_dir/PcrPolicy.crt" -pubkey -noout)
    secure_boot_certificate=$("${box[@]}" openssl x509 -in "$pkcs11_dir/SecureBoot.crt" -pubkey -noout)
    test "$pcrpkey" = "$pcr_certificate"
    test "$pcrpkey" != "$secure_boot_certificate"

    "$buck" run "${config[@]}" tine//examples/image-secureboot:vm-smoke
}

# Nothing here needs an engine, so a graph that does not analyze is reported in seconds rather than
# after two bootstraps. `check` runs it again, for anyone running that on its own.
group graph             -- "$buck" bxl tine//tools/graph.bxl:analyze
# First invocation fetches buck's pinned tools and builds the shared engine; kept its own group so
# bootstrap time stays visible.
group engine            -- "$buck" build tine//catalog:fedora.rawhide.engine
group check             -- "$buck" run tine//tools:check
# Every repository the catalog declares is pinned to a mirror serving immutable snapshots, so the whole
# catalog is verifiable rather than the engines that happen to be pinned.
group verify-catalog    -- "$buck" run tine//tools:verify-catalog
# Everything the cell declares, rather than the handful of targets someone remembered to name here:
# every example image over both package systems, the boxes, and the source-build demos.
group build             -- "$buck" build tine//...
group uki-modules       -- uki_modules
group boot-artifacts    -- boot_artifacts
group sysext-name       -- sysext_name
group rust-sbom         -- rust_sbom
group go-sbom           -- go_sbom
group secureboot-sbom   -- secureboot_sbom
group release-artifacts -- release_artifacts
# The boot smokes, which `check` above left out because they take minutes each. Adding one is
# declaring it, not naming it here as well.
group smokes            -- "$buck" test tine//... --include vm
group secureboot-pkcs11 -- secureboot_pkcs11
