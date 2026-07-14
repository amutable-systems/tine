# Approaches to self-hosting the BuildRequires mega-SCC

The ~69-package BuildRequires cycle (gcc ↔ glibc ↔ systemd ↔ kernel ↔ …, see the appendix below)
cannot self-host under the current lock: its intra-cycle edges are dropped to the seed, so the core
always builds against upstream rawhide binaries. Three candidate approaches. They sit on one axis:
**where does "the previous round" live?**

- **A** (overlay forever): nowhere — the previous round is permanently *Fedora's*.
- **B** (compose seed): outside the graph, in published state, iterated by publish cycles.
- **C** (two-stage): inside the buck graph, as explicit `P.stage1` targets.

## A. Never self-host the SCC — rawhide + our overlay, forever

Today's main, declared permanent: SCC members keep building against the seed; our repo is an
overlay of rebuilt packages on top of rawhide; periodic re-import/re-pin tracks upstream.

**Pros:**

- Zero new machinery, zero double builds. The gcc-edit cascade disappears: world-rebuilds happen
  only on seed refresh — a deliberate, batched event.
- Operationally proven shape (ublue, Hyperscale-style overlay distros).

**Cons:**

- **Provenance is bound to upstream forever.** Every binary is shaped by Fedora's compiler; "built
  by tools we built" is permanently off the table.
- **Build-against/run-against drift grows with divergence.** SCC members build against *seed*
  glibc/openssl but ship alongside *ours* in images. Today ≈ no-op (near-verbatim imports); every
  CVE patch or config divergence in a core package widens the gap. koji tolerates this class of
  skew only *transiently* (until compose refresh); This approach makes it permanent and unbounded.
- **Maximal coupling to the rawhide treadmill.** Rawhide's composes get garbage-collected
  within days (hence frequent refreshes). A commits to that treadmill at full intensity forever —
  the SCC's entire buildroot supply stays upstream, so mass rebuilds, soname bumps, and breakage
  arrive on Fedora's schedule, not ours.
- Security attestation noise: a gcc or glibc CVE fix ships in images while the *build environment*
  keeps the vulnerable seed package until the next re-pin. Fortunately these are rare.
  But this becomes worse in the forseeable future when the kernel starts depending on Rust.

## B. Publish our builds as a compose; seed = rawhide ∪ ours

What koji literally does: the mutable build tag expressed the legitimate way: as external pinned
input, iterated by publish cycles. Build world against seed_N (rawhide + our compose from round
N−1), publish → compose_N, re-pin, repeat; converges to self-hosted after one cycle.

**Pros:**

- **Self-hosting converges for free.** After one publish+re-pin, intra-SCC caps resolve from our
  compose (repo priority) instead of rawhide. No stage targets, no per-package lock surgery — the
  SCC-drop-to-seed rule stays *unchanged*; the seed just contains us. Rounds 3, 4, … come free
  with each publish; publish-over-publish diffs give the reproducibility metric.
- **The cascade becomes schedulable.** A gcc edit rebuilds gcc *once* (against the published
  previous gcc); the world rebuild happens at the next publish+re-pin — batched, deliberate,
  koji-style. Major operational win over C's immediate in-graph cascade.
- **Decouples from rawhide progressively**: once self-hosted, only the not-yet-imported fringe
  depends on upstream availability; the compose is under our retention control. Directly serves
  the cold-rebuildability invariant that trailing-rawhide only mitigates.
- The glibc32 special case dissolves after round 1 — our compose carries glibc32, like koji's
  buildroot repo.
- A publish pipeline may be desired eventually anyway (mirrors, third-party image builds, etc.)

**Cons** (these hit stated invariants, not just convenience):

- **External mutable state enters the build.** Same commit + different compose = different bytes.
  Determinism survives only if the compose is pinned like any repo (sha256 repodata — machinery
  exists), but the pin is **self-referential**: repo@N pins artifacts built by repo@N−k. Cold
  rebuild then means either *trusting the published compose as a root of trust* (weakening
  rebuild-from-source) or replaying the entire publish chain from day 0 (which is not practically
  feasible after a few months already).
- **Weakens buck-native ordering**: within a round, nothing forces glibc to build before gcc;
  that's correct, since gcc resolves the *published* glibc. But that also means intra-SCC artifacts
  no longer flow through buck dependency edges (where buck builds a provider right before its
  consumers, and its cache doubles as the package archive); they flow through the published compose,
  so a provider change reaches its cycle peers only at the next publish + re-pin, one koji-style
  round later. A hybrid approach keeps the acyclic lock for intra-round freshness and uses the
  compose only for intra-SCC caps, exactly like koji+compose refresh semantics.
- Round 1 still needs a bootstrap story (empty compose → glibc32 unsatisfiable), so the kept-edge
  mechanism (`buildroot_only_packages`) — or a manual seeding — survives regardless.
- **Publishing trips the deferred disciplines**: Needs to do `Release:` bumps for *all* releases
  (NEVR become global alias for "the bits in the rpm") and GPG signing the rpms. Not a real
  disadvantage, but setting up trustable RPM signing is non-trivial infra work.

## C. Fully self-host in-graph — two-stage builds

Spec'ed in commit 978ee9fcd3d7e1ee9a31676930de1a205d8489da on `buildreqs` branch, not landed on main
yet or implemented.

Each nontrivial-SCC member `P` splits into a seed-only `P.stage1` and a final `P` that overlays
the stage-1 builds of its cycle peers plus its own stage1 (the stage1-self-edge), reusing the
existing extra-packages overlay and repo-priority machinery. Round 2 is the published fixpoint.

**Pros:**

- **Pure buck: no external state at all.** The whole bootstrap is one deterministic graph
  evaluation — same commit, same bytes, cold-rebuildable from committed pins + upstream alone.
  The only approach with *from-scratch* reproducibility of a self-hosted world; no self-referential
  pins, no archive as root of trust.
- **Strongest provenance, immediately**: everything published is built by tools we built, within
  a single evaluation — no publish cadence between a core fix and a fully self-hosted world.
  Trusting-trust story is as tight as it gets short of diverse double-compilation.
- **No new infrastructure**: no publish pipeline, signing, or retention policy; buck's cache remains
  the archive.
- Implementation is small and contained (rpmjson.bzl: stage-target emission + name knob).

**Cons:**

- **The cascade is immediate and unavoidable.** Any change reaching `gcc.stage1`'s inputs (gcc
  itself, glibc via the glibc32 kept edge) rebuilds every SCC final *now*, in-graph; there is no
  batching. Mitigated only by byte-identical early cutoff (a rebuild that doesn't change
  bytes stops the fan-out).
- **Double build cost of the core**: every SCC member builds twice; gcc alone is 1 h per
  pass on a laptop, 2 h for both stages. This is trimmed down with `build_gcc_basic`
  (unmodified Fedora package takes 10 hours to build). A core toolchain change pays both
  stages of the whole downstream cone. All other packages together (including the kernel)
  build in 2 hours.
- Fixed convergence depth: N=2 by decision ("round 2 = done"); a round-3 reproducibility compare
  is an extra deliberate build, not a free byproduct of operations (contrast B's
  publish-over-publish diffs).
- Purity gap in round 2: providers-only overlay leaves non-BR'd transitive members (notably glibc)
  seed-supplied. This is a (changeable) decision, not inherent to this approach, see design.md's
  two-stage section.
- **Cannot express "bootstrap once, then sources-only"**: a pure graph has no memory, so the
  initial bootstrap is not an event that happens once — `P.stage1` permanently means "build
  against the seed" and re-resolves rawhide on every invalidation and cold rebuild. Persisting
  the bootstrap so later builds consume it *is* B's compose. The seed thus remains a live,
  load-bearing input forever, and the re-pin treadmill continues at full breadth (this is the
  flip side of the "no external state" pro: the graph can't remember a previous round).

## Synthesis

**B subsumes A** (A = B with publishing switched off), and **C is B's missing cold-start**: the
deterministic, external-state-free way to produce the *first* compose from pure rawhide — and to
re-derive it from scratch if the self-referential pin ever needs re-grounding. The three form
one story rather than compete:

1. Operate as **A** now (already the case).
2. Adopt **B** when the publish infrastructure and its NEVR/signing discipline are worth it.
3. Keep **C** documented-but-unimplemented (its current state) as the reproducible bootstrap under
   B and the answer to "can we rebuild the world from nothing without trusting our own archive."

The real decision is not which mechanism — it's **when publishing starts** and **whether
from-scratch reproducibility of the compose is a requirement or a nice-to-have**.

## Common limit: The buildroot closure is far bigger than our package set

With all approaches, "self-hosting" as discussed here covers the packages we *build*, but a
buildroot's install closure is mostly packages we don't import/build at all: build tools and their
dependencies (perl, python, texinfo, dejagnu, make, …) plus the `@buildsys-build` base. Concretely:
crypto-policies' buildroot is 346 rpms of which 10 are ours; gcc's is 1018 of which 7 are ours.
Everything else comes from rawhide. This is a conscious decision as we trust Fedora in general, but
want to keep control over the packages which are in the product and CVE relevant, but not
build/track packages which are e.g. only needed to build documentation (which we don't install
anyway).

So none of the approaches self-hosts the *build environment*; they self-host the *shipped set*
and (B/C) the toolchain that shapes its binaries. Fully self-hosting the environment means
importing the entire buildroot closure (hundreds more source packages) which is an import-scope
decision orthogonal to A/B/C. B at least improves progressively as the import set grows (each
newly imported package's binaries start outranking rawhide's in every buildroot after the next
publish); under C the stage-1 layer stays seed-resolved regardless.

## Common: The re-pin treadmill is an availability problem, fixable by archiving

The treadmill mentioned above is also common to all three approaches, and orthogonal: it has two
distinct drivers. One is *wanting* updates (tracking rawhide for CVE fixes and new versions) —
that's voluntary and stays under any approach.

The other is *forced* re-pins: rawhide's compose is garbage-collected on every push, so package
locations recorded by an authoritative repository snapshot can disappear within days even if we
wanted to change nothing, and cold-rebuildability breaks. That is purely an availability problem,
and mirroring/archiving the snapshotted RPM pool fixes it under A, B, and C alike: rawhide is then
touched only at deliberate import/refresh time. The cost is mostly storage infrastructure.

# Appendix: Current fedora/rawhide mega-SCC

Computed from the branch srcpkg.json metadata (75 source packages) with the same edge derivation as
rpmjson.bzl's `_buildroot_locks` (provides = binary names ∪ Provides ∪ Files; X → P iff X's
BuildRequires intersect P's provides; rich deps tokenized). One nontrivial SCC.

- acl
- attr
- audit
- bzip2
- ca-certificates
- cryptsetup
- curl
- dbus
- dosfstools
- e2fsprogs
- elfutils
- expat
- gcc
- glibc
- gmp
- gnupg2
- gnutls
- json-c
- kernel
- keyutils
- kmod
- krb5
- libarchive
- libassuan
- libbpf
- libcap
- libcap-ng
- libeconf
- libevent
- libffi
- libgcrypt
- libgpg-error
- libidn2
- libkcapi
- libksba
- libnsl2
- libseccomp
- libselinux
- libsepol
- libtasn1
- libtirpc
- libtool
- libunistring
- libusb1
- libverto
- libxcrypt
- libxml2
- lvm2
- lz4
- ncurses
- nettle
- nghttp2
- npth
- openldap
- openssh
- openssl
- p11-kit
- pam
- pcre2
- readline
- setup
- sqlite
- systemd
- tpm2-tss
- tzdata
- util-linux
- xz
- zlib-ng
- zstd

For comparison, these six are part of the imports and on the current AOS image, but *not* in the SCC:

- crypto-policies
- dbus-broker
- filesystem
- libsemanage
- shadow-utils
- wireguard-tools
