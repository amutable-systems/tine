# Signing with external keys

The development path, where key material is a PEM file in the build graph, is described under "Secure
Boot signing" in [images.md](images.md).

This describes the production case: external signing keys (preferably on hardware), used by the
[build over a socket](https://p11-glue.github.io/p11-glue/p11-kit/manual/remoting.html). The builds never
get access to the keys themselves.

Three signing roles can each take such a key, independently:

| Role         | What it signs                                        | Tool                        |
|--------------|------------------------------------------------------|-----------------------------|
| Secure Boot  | the UKI, its kernel, systemd-boot, ESP enrollment    | `systemd-sbsign`, `bootctl` |
| expected PCR | the TPM policy sealing PCR 11, per UKI profile       | `systemd-measure`           |
| verity       | disk/sysext `*-verity-sig` partitions                | `systemd-repart`            |

`image_sysext`/`sysext_image` take a verity-role key for their DDI as well. The host merging the extension
validates the signature against the certificates in its `/usr/lib/verity.d/`; these must be files: a
token's certificate is a URI, extract it with `/usr/lib/systemd/systemd-keyutil extract-certificate
--certificate-source provider:pkcs11 --certificate '<uri>'` (PEM on stdout).

## How the pieces connect

The build sandbox has no PKCS#11 module for the key and no access to the hardware. Instead, it has a client
that forwards PKCS#11 calls over a Unix socket:

```
build sandbox                        │ host
                                     │
systemd-sbsign / systemd-measure     │
  └─ OpenSSL                         │
      └─ pkcs11-provider             │   p11-kit server
          └─ p11-kit-client.so ──────┼──▶  └─ a PKCS#11 module
                       (unix socket) │          └─ the hardware
```

Only the socket crosses the boundary. The sandbox gets no device, no token store, and no access to any
object other than the tokens the server was told to expose. Replacing the module, or the whole server with a
remote signing service, changes nothing on the build side.

Two things do cross into the build that are worth pointing out:

- **The PIN.** `C_Login` happens on the client side, so the token PIN must be readable inside the sandbox.
  The boundary limits *what* the sandbox can use the key for, not who holds the PIN. Where the module maps
  the PIN onto a hardware secret, as `tpm2-pkcs11` does with an object's authValue, treat that secret as
  exposed to the build.
- **Unlimited signing operations** for the lifetime of the server, on the exposed tokens. The boundary is
  scope, not rate.

## Prerequisites

The build side needs nothing from you: the catalog installs `pkcs11-provider` and `p11-kit-client` into
every engine. On the host holding the key:

- `p11-kit-server` for the `p11-kit server` subcommand and its systemd units.
- your backend's module and its tooling: `tpm2-pkcs11` and `tpm2-pkcs11-tools`, or `opensc`, or `softhsm`.
- `gnutls-utils` for `p11tool`, and `pkcs11-provider` plus `openssl` for the certificate step.
- `p11-kit-client`, a separate package from `p11-kit`, for the socket check below.

## What tine needs

Signing coordinates are host-specific, so they are not committed. The `config_signing_key()` macro
declares a key that uses a PKCS#11 token when doing the build with appropriate configuration (see below),
and falls back to generating a development key pair without it. So one image definition serves both:

```Starlark
load("@tine//image:defs.bzl", "bootable_disk_image", "config_signing_key")

# section defaults to "signing", token_key defaults to "token"
config_signing_key(name = "secureboot")
config_signing_key(name = "pcr-signing", token_key = "pcr-token")

bootable_disk_image(
    name = "image",
    secure_boot_key = ":secureboot",
    sign_expected_pcr_key = ":pcr-signing",
    ...
)
```

`examples/image-secureboot` is the worked example of this shape, and `tools/ci.sh` builds it against
tpm2-pkcs11 tokens on a software TPM, served by `tools/signing-server` (which exercises the steps from
this document). The invocation supplies the host-side parameters:

```sh
tools/buck build \
    -c signing.token=SecureBoot \
    -c signing.pin-file="$HOME/.config/signing-pin" \
    -c signing.socket="$XDG_RUNTIME_DIR/signing/pkcs11" \
    //your:image
```

Rather than repeating those on every invocation, your build infrastructure can place them in the
`.buckconfig.local` of the parent OS.git cell, which Buck reads as an overlay over that cell's
`.buckconfig`. Keep it out of the repository via `.gitignore`:

```ini
[signing]
token = SecureBoot
pin-file = /home/build/.config/signing-pin
socket = /run/signing/pkcs11
```

A build host that materializes its coordinates elsewhere, like a systemd credential, points the build at
that file instead. Nothing about the host then enters the tree, and unlike a file in a cell, it applies
to every cell:

```sh
tools/buck build --config-file "$CREDENTIALS_DIRECTORY/signing.bcfg" //your:image
```

**Warning: Buck silently ignores a `--config-file` that does not exist**, and the build then signs with
development keys. Have the invoking job check for the file, or include it from `.buckconfig.local` as
`<file:/run/credentials/build.service/signing.bcfg>`, which fails when it is missing.

Each signing role (`secure_boot_key`, `sign_expected_pcr_key`, etc.) can get its own token and
configuration key. The example declares its expected-PCR key that way, so building it externally takes a
`-c signing.pcr-token=<label>` as well; the PIN file and socket are shared.

Underneath, the macro declares a `pkcs11_signing_key`, which a project with its own switching logic can
declare directly:

- `token`: the token label, used as `token=` in the generated URIs. From `<section>.<token_key>`.
- `object`: the key's `CKA_LABEL`, used as `object=`. Rule only; defaults to `token`.
- `pin_file`: host path to a file with the token PIN on its first line, from `<section>.pin-file`. It
  must be readable by the user running the build; it is bound read-only into the sandbox and referenced
  as `pin-source=` in the private key URI. There is no interactive alternative: a build action has no
  terminal to prompt on.
- `socket`: host path of the `p11-kit server` socket, from `<section>.socket`.

All paths are absolute and resolved by the invoker, not by tine.

**Give the socket a directory of its own**, holding nothing else: a socket cannot be bind-mounted on its own,
so the sandbox gets the whole directory it sits in. `$XDG_RUNTIME_DIR/p11-kit/` is the tempting choice and
the wrong one, since that is where other servers put their sockets too.

Roles may share one key target or use separate ones, and a role without an assigned key is not signed.
The Secure Boot and expected-PCR keys must share one source and one socket due to how `ukify` works. Only
the verity key may sit behind a different socket.

Signing actions with an external key run locally and never reach a shared cache, because their output
does not follow from their declared inputs. For the same reason Buck does not know when the token's
content changes: it skips the action while its declared inputs are unchanged, so new key material behind
an unchanged URI is not picked up until an input changes, the daemon restarts, or `buck clean`. Keep
development images on PEM keys to stay cacheable.

## Setting up a token

Pick the backend you have. What each needs to end up with is the same: a token with a label, a user PIN you
know, and an RSA or EC private key in it.

**A Secure Boot key must be RSA.** Firmware verifies PE signatures with RSA, so an ECDSA key signs happily
and then fails to boot. An EC key can still serve as the expected-PCR key, since the TPM verifies that
signature rather than firmware. Verity signatures are verified by systemd, which accepts both.

### Listing existing tokens

[`p11-kit list-modules`](https://p11-glue.github.io/p11-glue/p11-kit/manual/p11-kit.html) shows the
configured modules and their tokens;
[`p11tool`](https://man7.org/linux/man-pages/man1/p11tool.1.html) lists a token's objects as full URIs,
which is where `token=` and `object=` come from:

```sh
p11tool --list-all --login "pkcs11:token=<token>"
```

The two labels are usually the same string (but nothing requires that). The listing is also where you see
whether a certificate already sits next to the key.

**Note**: Every listing of a tpm2-pkcs11 token warns `Needed CKA_VALUE but didn't find encrypted blob`,
which is not an error: `p11tool` asks for an attribute that only exists for objects the token stores
wrapped under its own key, which a TPM key pair is not.

### Backend: TPM 2.0 with tpm2-pkcs11

Keys are created inside the TPM and never exist anywhere else. See
[INITIALIZING.md](https://github.com/tpm2-software/tpm2-pkcs11/blob/master/docs/INITIALIZING.md).
**Give signing its own store** rather than adding a token to an existing one, so that the server exposes
only the signing token and its PIN stays separate from anything else:

```sh
export PKCS11_PROVIDER_MODULE=/usr/lib64/pkcs11/libtpm2_pkcs11.so
export TPM2_PKCS11_STORE=/var/lib/signing-pkcs11
mkdir -p "$TPM2_PKCS11_STORE"

tpm2_ptool init                          # a fresh store gets one primary object, id 1
tpm2_ptool addtoken --pid=1 --label=SecureBoot --sopin="<sopin>" --userpin="<pin>"
tpm2_ptool addkey --label=SecureBoot --key-label=SecureBoot --userpin="<pin>" --algorithm=rsa2048
```

Everything that loads this module needs `TPM2_PKCS11_STORE` in its own environment, `p11-kit server`
included; without that, it looks in various system/user default directories.

Expect cosmetic FAPI errors on a host with no `/etc/tpm2-tss/fapi-config.json`.

#### Starting over

Deleting the store takes the tokens and their keys with it, but not all of the state: `tpm2_ptool init` makes
its primary object **persistent**. Persistent handles occupy limited TPM NV space, so one leaked per
iteration eventually runs the TPM out. Release the handle first, then delete the store:

```sh
tpm2_ptool destroy --pid=<id>
rm -r "$TPM2_PKCS11_STORE"
```

**`destroy` is broken before 1.9.2**, i.e. in Fedora 44 (1.9.1) and CentOS 10 (1.9.0). It fails with
`IndexError: No item with that key`. This is
[fixed upstream](https://github.com/tpm2-software/tpm2-pkcs11/pull/883); see that PR for a workaround.

### Backend: smartcard or HSM with OpenSC

For a token exposed by OpenSC, such as a Nitrokey or a PIV card. The device is initialized with its vendor's
own tooling, which also sets the label and PINs; after that key creation is generic
([pkcs11-tool(1)](https://man.archlinux.org/man/pkcs11-tool.1.en)):

```sh
export PKCS11_PROVIDER_MODULE=/usr/lib64/pkcs11/opensc-pkcs11.so
pkcs11-tool --login --keypairgen --key-type rsa:2048 --label SecureBoot
```

### Backend: a software token, for testing

`softhsm2` needs no hardware, so it is the way to exercise this path on a machine with no TPM. The keys are
files on disk, so use it only for tests.

```sh
export PKCS11_PROVIDER_MODULE=/usr/lib64/pkcs11/libsofthsm2.so
softhsm2-util --init-token --free --label SecureBoot --so-pin "<sopin>" --pin "<pin>"
p11tool --login --generate-privkey=rsa --bits=2048 --label=SecureBoot "pkcs11:token=SecureBoot"
```

Its tokens live in the `directories.tokendir` of its configuration
([softhsm2.conf(5)](https://man.archlinux.org/man/softhsm2.conf.5.en)), `/var/lib/softhsm/tokens` as Fedora
ships it. Starting over is `rm -r` on that directory; nothing lives outside it.

## Giving the key a certificate

You need to generate an X.509 certificate for a freshly created key. `openssl` takes the module from
`PKCS11_PROVIDER_MODULE` exported above, and prompts for the PIN:

```sh
openssl req -new -x509 -days 3650 -sha256 \
    -provider pkcs11 -provider default \
    -key "pkcs11:token=<token>;object=<key>;type=private" \
    -subj "/CN=<token> signing key/" \
    -out cert.pem
```

Check the subject and public key of what came out (`openssl x509 -in cert.pem -noout -text`) before going
further: nothing later in the signing stack checks that a certificate and a private key belong together, so
a certificate over the wrong key yields signatures nothing can verify.

Store it in the token, so the build can address it as `type=cert` instead of needing a file. Prefer the
backend's own tool where there is one, because it copies the key's `CKA_ID` onto the certificate; for
TPM:

```sh
tpm2_ptool addcert --label=<token> --key-label=<key> cert.pem
```

Otherwise, `p11tool --login --write --load-certificate=cert.pem --label="<key>" "pkcs11:token=<token>"`
is the generic alternative, and a `type=cert` URI in `p11tool --list-all` confirms it landed.

`cert.pem` can be thrown away once it is in the token. `p11tool --export
"pkcs11:token=<token>;object=<key>;type=cert"` can read it back. Export it that way rather than minting a
replacement: re-running `openssl req` produces a *different* certificate over the same public key, and
Secure Boot enrollment trusts a certificate rather than a public key, so once anything has enrolled it, a
regenerated one is a different trust anchor.

## Serving the token

Write the PIN to a file the build can read, make a directory for the socket, then start the server. All
of it runs as the user invoking the build, since the sandbox adds no privilege.

```sh
# race-free creation of a 0600 file
install -m 0600 /dev/null "$HOME/.config/signing-pin"
echo "<pin>" > "$HOME/.config/signing-pin"

mkdir -p -m 0700 "$XDG_RUNTIME_DIR/signing"
eval "$(p11-kit server --provider "$PKCS11_PROVIDER_MODULE" \
        --name "$XDG_RUNTIME_DIR/signing/pkcs11" "pkcs11:token=SecureBoot" "pkcs11:token=PcrPolicy")"
```

Like `ssh-agent`, the server daemonizes and prints assignments for `P11_KIT_SERVER_ADDRESS` and
`P11_KIT_SERVER_PID`; the latter is read by `p11-kit server --kill`; `--name` creates the fixed
socket path that gets passed to `-c signing.socket`.

**Name the tokens.** One server can expose several tokens as shown above: specify them as additional
`pkcs11:token=...` URI matches. A bare `pkcs11:` matches everything p11-kit knows, including unrelated
tokens such as your SSH keys. The user units which the `p11-kit-server` package ships do exactly that, so
serving a signing token through them requires `ExecStart` and `ListenStream` drop-ins.

## Checking/Debugging the socket before involving the build

Signing through a forwarded PKCS#11 socket is not a well-trodden path, and error messages are often
generic and unhelpful. Confirm the two operations that matter, with only the client module and the socket
address set, so a failure here is unambiguous. Probing first also protects the token: every signing
action logs in on its own, so one build turns a stale PIN into several failed attempts at once, against a
smartcard's retry counter or the TPM's dictionary-attack lockout.

This is the one step that does **not** use the backend module. It stands in for the build, so it points
at the client instead, exactly as the sandbox does. **Use a fresh shell**: overwriting
`PKCS11_PROVIDER_MODULE` here means the earlier backend-direct commands no longer work in this one.

```sh
export PKCS11_PROVIDER_MODULE=/usr/lib64/pkcs11/p11-kit-client.so
export P11_KIT_SERVER_ADDRESS="unix:path=$XDG_RUNTIME_DIR/signing/pkcs11"

KEY="pkcs11:token=<token>;object=<key>;type=private?pin-source=$HOME/.config/signing-pin"
CERT="pkcs11:token=<token>;object=<key>;type=cert"

/usr/lib/systemd/systemd-keyutil validate \
    --private-key-source provider:pkcs11 --private-key "$KEY" \
    --certificate-source provider:pkcs11 --certificate "$CERT"

cp /usr/lib/systemd/boot/efi/systemd-bootx64.efi /tmp/probe.efi
/usr/lib/systemd/systemd-sbsign sign \
    --private-key-source provider:pkcs11 --private-key "$KEY" \
    --certificate-source provider:pkcs11 --certificate "$CERT" \
    --output /tmp/probe.efi.signed /tmp/probe.efi
```

Per [systemd-keyutil(1)](https://www.freedesktop.org/software/systemd/man/latest/systemd-keyutil.html),
`validate` checks that both can be *loaded*, which through this socket is the interesting part; it does not
compare them, and neither does anything else in the signing stack.
[systemd-sbsign(1)](https://www.freedesktop.org/software/systemd/man/latest/systemd-sbsign.html) `sign` then
proves `C_Login` with the PIN file and a real signing operation. Together they cover everything the build
does with the key.

When something fails, start with a plain listing, which separates a socket or module problem from a missing
object. You have to specify `--provider` on the command line:

```sh
p11tool --provider "$PKCS11_PROVIDER_MODULE" --list-all "pkcs11:token=<token>"
```

If the token and its objects appear here but a URI still fails, the object that URI names does not exist:
OpenSSL reports a PKCS#11 object it cannot find as `No such file or directory`, and a `type=cert` URI
failing that way usually means the certificate step above was skipped. If nothing appears, the client is
not reaching the server: check that `PKCS11_PROVIDER_MODULE` is exported in *this* shell, that
`p11-kit-client` is installed at all, and that the server is still running. Two lines in systemd's debug
output are noise rather than causes: `libcrypto.so.4 is not available` is a probe that falls back to
`libcrypto.so.3`, and an `error:...:pkcs11::General Error` beside the failure at least means the provider
itself loaded.
