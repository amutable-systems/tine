# tine's remote build cache

The [Buck2](https://buck2.build/) build system can ask a remote cache via
[RE-API](https://buck2.build/docs/users/remote_execution/) whether an action has already been run, and
take its outputs instead of running it. tine implements such a cache over an S3 bucket, with properties
that existing implementations such as [bazel-remote](https://github.com/buchgr/bazel-remote) or
[NativeLink](https://github.com/TraceMachina/nativelink) don't provide: not trusting the cloud provider,
being robust against data corruption, reading with plain HTTP/public buckets, and functioning with dumb
bucket expiry rules.

## Threat model

**The bucket operator is not trusted.** The storage provider, whatever fronts it, and anyone who ever
obtains the write key. Assume they can read, rewrite, delete, reorder and replay any object at will.

**The production build machine is trusted, and can poison the cache.** It runs the actual build actions
and signs their results as well as the uploaded cache entries. A compromised builder can already put
arbitrary code into the produced and published images, and sign them with its keys. The cache adds no
additional exposure. The way to guard against that is keeping image builds reproducible, so that anyone
can rebuild and compare.

**A developer machine is not trusted.** They only get read access and no trusted signing certificate, and
thus cannot poison the cache.

**Pull requests are safe only on a hermetic builder.** The action digest covers the command and every
declared input, so a branch that changes any of them builds under a different key and cannot overwrite
what main built. But it can overwrite it through an action that reads something its rule did _not_
declare: another file in the checkout, the network, or even the clock. Such an action produces a
different output under an unchanged key, and a builder running the branch signs and publishes it under
the key main uses. tine's sandbox removes the host and the network from what an action can read, but
binds the whole checkout, so an undeclared project file is not caught; the guarantee rests on every rule
declaring what it reads. Readers cannot cause any of this, since they never write.

So a builder with a write key builds trusted branches, and pull requests only after review. Pull requests
from anyone can be built on a builder without a write key: a malicious branch then costs compute, not the
cache.

## Concept

Terms, as RE-API and this document use them:

- **Action**: one command with its inputs. Buck2 hashes both into the SHA-256 **action digest**; any
  changed input is a different action.
- **Result**: what an action produced: a list of its outputs (files, directory listings, stdout,
  stderr), each entry is the output's path and the SHA-256 digest of its bytes. The bytes themselves
  are not in the result; the digest is how they are looked up in the bundles. RE-API calls this an
  [`ActionResult`](https://github.com/bazelbuild/remote-apis/blob/main/build/bazel/remote/execution/v2/remote_execution.proto).
- **Bundle**: one bucket object: a container with one **member** per output (see "Bundle container
  format" below), named by the output's digest and holding its bytes. The bundle is named by the **set
  digest**, the hash of the sorted member digests.
- **Pointer**: the bucket object a reader looks up by action digest. It holds the result and the name of
  its bundle, and is signed by the builder.
- **Shim**: the build cache process on each machine that Buck2 speaks RE-API to. It keeps a local store
  of results and output bytes, and is the only thing that touches the bucket. A **builder** is a shim
  with a write key and a signing key, a **reader** has neither.

Read path:

- Buck2 computes the action digest `d` and asks the shim for its result.
- Shim has the result locally: return it.
- Shim `GET`s the pointer `ac/<d>`. Missing: cache miss. Otherwise verify the builder's signature over `d`
  and the pointer's content, and that the signing certificate (`GET keys/<key id>` when unknown) chains
  to the configured authority. Failure: miss, logged.
- Every output the result names is already local: return the result. Otherwise `GET` the bundle the
  pointer names, in full. Check that its members are exactly the outputs the result names and that each
  matches its digest. Store them locally, return the result. Missing, or any check failing: miss.
- Buck2 reads the outputs it wants from the shim's local store on a hit, or runs the action on a miss.

Two GETs per hit, no HEAD, no LIST, no S3 API.

Write path, builder only:

- Buck2 uploads the output bytes to the shim, which verifies each against its digest and stores it.
- Buck2 hands the shim the result for `d`.
- Shim packs the outputs the result names into a bundle and computes the set digest. A bundle the bucket
  already has gets its date refreshed, otherwise `PUT bundle/<set digest>`. No HEAD first: two builders
  racing on the same bundle write the same bytes.
- Shim signs `d` plus result and bundle name, `PUT`s that as the pointer `ac/<d>`.

Two PUTs per cached action, nothing per output. Writes happen via the S3 API with a write key.

## Bucket layout

- `ac/<action digest>`: the pointers: `signature || { bundle name, result }`
- `bundle/<set digest>`: the bundles
- `keys/<key id>`: the certificate of a signing key

Flat keys under these three prefixes, no sharding (not necessary any more with recent providers). The
read path is identical whether it is served by a bucket's public domain, a static file server, or
something local. Sharding is done on the local file system side, as that works better with Linux file
systems (directory index lookups and `readdir` over hundreds of thousands of entries work poorly).

`<set digest>` is the hash of the **sorted set of member digests**, not of the container bytes; so two
actions with identical outputs share one bundle, a result naming one output under two paths shares it
too, and the container encoding can change without renaming anything.

Whatever fronts the bucket may cache `bundle/` freely: those objects are immutable, and a bundle is named by
its content. It must not cache `ac/` or `keys/`: they are rewritten in place, and it must not cache a 404
for anything.

This was checked with [Cloudflare R2](https://www.cloudflare.com/products/r2/): its `r2.dev` domain caches
none of these. It is also rate-limited and documented as not for production, so a big deployment needs a
custom domain with caching left off for those two prefixes.

The domain should serve every object as `application/octet-stream` with `X-Content-Type-Options:
nosniff`. The shim never looks at a content type, but whoever holds the write key can store HTML under
any key with a content type of their choosing, and a public read domain that serves it as such is a place
to host phishing pages. That is the one thing bucket write access buys that is not about the cache.

Whether the bucket is public is the deployment's choice. A cached result is the bytes anyone gets by
building the repository locally and contains the pieces of a built image, so the bucket should be private
exactly if the repository and its published images are both private.

A public bucket needs only a base URL on the read side (developer machines). A private one needs a read
token, and against R2 that means SigV4-signed GETs, since its S3 endpoint has no bearer-token mode. This
is not currently implemented, but can easily be done when needed: reads are done in `Reader.get(key)` in
`bucket.py`, which can be extended.

There is deliberately no buck dep-file cache in the bucket. Its entries are an optimisation whose miss
falls through to the ordinary lookup and then to execution. This is kept local-only, a cold start loses
nothing but a little speed. Keeping them out also removes a forgeable key space: those keys are *not*
action digests, so anything shared would need its own signing rule.

## How this design prevents attacks

| Attack | What stops it |
|---|---|
| Rewrite a member | every member is checked against its own digest |
| Change or add members of a bundle | the members must be exactly what the signed result names |
| Point a result at a different bundle | the pointer is signed, and the bundle name is inside it |
| Move a valid pointer to another action digest | the action digest is in the signed message |
| Strip the signature to look unsigned | a reader holding keys refuses an unsigned object |
| Serve their own certificate and results | the certificate must chain to an authority given out of band |
| Replay an older pointer under its own action | nothing, and nothing needs to: see below |
| Replay a pointer signed by a key since retired | that key's certificate must be valid now |
| Copy a pointer from a bucket sharing the CA | nothing: one authority is one bucket, below |
| Delete anything | nothing. It just costs a rebuild |

The replay row is not a gap. An action digest covers the command and every input, so an older result filed
under it is a result of *the same action*, and serving it is what a cache is for. What replay does buy an
attacker is a result signed by a key that has since been retired, and requiring that key's certificate to
be valid now is what closes it.

The members are also checked to hash to the name the bundle is stored under. That catches a builder that
filed a bundle under the wrong name, not anything an operator could do.

Out of scope: denial of service by an operator who deletes or throttles the bucket, traffic analysis over
which actions a developer looks up, and a compromised builder.

## Bundle granularity

Fetching the whole bundle before answering removes an entire problem class a per-output layout (such as
bazel-remote or NativeLink) has. There is no window between checking that the outputs exist and fetching
them, so nothing can be stale and no reference can dangle: The fetch is the existence check. An age
expiry rule on the bucket cannot break a build: a pointer whose bundle is gone is a miss. And no index
from output digest to bundle is needed for a hit, because after unpacking, the local store *is* the
index.

This is affordable because a bundle is a single action's outputs, not the whole cache. The total may grow
to gigabytes; what matters is the largest single bundle. For that to work well, the build system needs to
limit/split the maximum result size, i.e. opt into caching results like individual rpm/cargo/Go project
builds which are expensive to compute, but have fairly small (MB range) results. It is not advisable to
cache entire disk images, as they take longer to download from a cache than to build on a local disk.

A bucket that answers badly, a 403 or a 502, is an error and a miss, never "not cached", and is asked
again next time. One that cannot be reached at all is left alone for a minute and every lookup meanwhile
is a miss that says so: a laptop behind a captive portal, or a network that drops rather than refuses,
would otherwise wait out a timeout per action.

## Publishing

**Upload bundle before pointer.** A pointer whose bundle is not there yet is the one ordering that can be
observed as a broken hit.

**Refresh instead of HEAD.** Actions whose digests differ but whose outputs do not are the usual case after
a configuration change, so the builder asks the bucket for the bundle before uploading it. The question is
a **server-side copy of the bundle onto itself**: it costs the same as a HEAD and re-dates the object. A
bundle is written once however many results share it. Without a refresh, the most shared bundles would
be the first to expire, out from under pointers written yesterday. Refreshed on every reuse, a bundle is
as old as its newest pointer. The probe and the upload are under one lock per bundle name, because results
that finish together often share a bundle, and a publisher that assumed another would finish could write
its pointer first.

**Expiry** is the bucket's own age rule. Requirements:

 - `ac/` and `bundle/` share one lifetime, since a bundle is never older than its newest pointer; a
   bundle lifetime shorter than the pointer's would result in dangling pointers. A pointer whose bundle
   has gone anyway is still only a miss, but it breaks the cache's effectiveness.
 - `keys/` may share that lifetime only if it is at least the leaf's validity: the certificate is
   written when a builder starts and needed until it expires.

The builder gets told the lifetime, so that it stops signing before a result would outlive its
certificate.

## Bundle container format

Simple concatenation of members: each a fixed header (`HEADER` in `bundle.py`: digest and size) followed
by the bytes. The member name is the output's digest, so the container is self-describing and needs no
manifest inside it. It is not tar because a general archive format has features (sparse members, links,
size fields past the object) which attacks can abuse, and we don't need. The format sits behind a
`pack`/`unpack` pair, and the bundle name covers the member set rather than the bytes, so the container
format can be swapped without renaming anything.

No compression yet. The bundle's encoding is invisible to Buck2, so compressing it is a local decision
about download time against CPU, and nothing measured so far has needed it: the results of actions which
opt into remote caching mostly consist of already compressed rpms, and compressing Go/Rust binaries does
not make enough of a difference yet. If/when compression is desired: zstd inside `pack`/`unpack` is one
flag and no renames. Separately, the shim advertises no RE-API compressor, which keeps Buck2 on plaintext
ByteStream and its 4 MB batch default.

## The local store

Buck2 asks for outputs one **blob** at a time, the bytes of a single output under its digest, often in a
later build than the one that looked up the **Result**. The S3 bucket stores **Bundles**, so something
local has to hold the unpacked bundle members and remember which bundle each came from: the bucket only
knows whole bundles under set digests and cannot answer "give me this blob" at all.

That is a directory, passed to the shim by `tine`. On a developer machine that is
`$XDG_CACHE_HOME/tine/cache`, `~/.cache/tine/cache` by default, where it is fine to lose and also will
only grow up to 1 GB. A builder gets a dedicated long-lived directory outside any per-job scratch space,
so that the next job does not start cold.

Output bytes are files. Bounded by size and evicted least recently used. Losing one costs a bundle fetch.

Everything else is a row in SQLite: results, and which bundle carried an output. They are bounded by
count and outlive the bytes they describe, so an evicted output can be fetched again from its bundle when
Buck2 asks for it by digest.

One process holds the store at a time, and the store is disposable.

The one gap of the eager fetch: Buck2 may ask for an output by digest in a later build, and if its bundle
has left the bucket by then, that build fails with Buck2's "expired in the RE CAS" error; `buck2 clean`
recovers. Rarer than with per-output storage, not gone.

See `remote_cache/store.py` and its comments for details.

## The signature

Only the pointer is signed; everything else in the bucket is named by its content. Two byte strings:

- **Signed** with Ed25519: `"tine-cache-ac-v1\0" || <action digest, 64 hex> || payload`. This is never
  stored. The reader rebuilds it from the action digest it asked for and the payload it received, so a
  pointer cannot be moved to another action, and the domain prefix keeps the signature from meaning
  anything outside this cache.
- **Stored** as the pointer `ac/<action digest>`:

  ```
  "TINE" | version u8 | algorithm u8 | key id [8] | signature length u16 | signature | payload
  ```

The payload is a small protobuf message: bundle name and serialised result. The key id is the
first 8 bytes of the SHA-256 of the key's DER `SubjectPublicKeyInfo`, which `openssl pkey -pubin -outform
DER | sha256sum` prints, so an unknown one in a log can be looked up. Ed25519 is the only algorithm; the
algorithm byte and the signature length are written anyway so that a future second one is just a new
constant, not a format change.

Two startup checks keep a misconfiguration from becoming a silent downgrade:

- A builder is given the same `--authority` certificates as a reader, and refuses to start when its own
  leaf certificate does not chain to one of them, or when it has a signing key and no `--authority` at all.
  Either would fill a cache nobody can read.
- A reader refuses to start without an authority. Forgotten keys must never turn into trusting the bucket
  operator. (Note: there is an explicit `--unsigned` option for local testing).

## The keys

Two keys per builder. The **CA key** lives in the build server's TPM and never leaves it; it signs the
leaf certificate, once per rotation. The **leaf key** is an ordinary Ed25519 key file on the same machine;
it signs every pointer. There is no flat list of trusted keys: a reader is given CA certificates
(`--authority`) and nothing else.

**Use a separate CA per bucket.** A reader accepts any leaf its CA has ever issued, so a CA shared between
a staging and a production bucket would let whoever writes both copy pointers from one to the other. The
CA issues leaf certificates for its one bucket and nothing else.

**Why two levels.** TPM signing is too slow for hundreds of results per build, and a TPM has no Ed25519.
A leaf key on disk is acceptable because the machine is already trusted to sign releases, and unlike the
CA key the leaf expires. Ed25519 because it is small, fast, and one algorithm means one code path.

**Distribution.** The builder publishes its leaf certificate to `keys/<key id>` at startup. A reader
fetches an unknown key id once, validates the chain and remembers it, so rotating the leaf changes nothing
on any reader. A remembered certificate that has expired is fetched once more before a pointer is refused:
a renewed certificate keeps the key and so the key id, and the bucket may hold a newer one.

**What a reader checks on a leaf**: it chains to a configured CA, it is valid now by the reader's own clock
(on every use, not once when fetched), it is not itself a CA, it has `KeyUsage digitalSignature`, and it
holds the key it was filed under. The key id only selects the certificate; the signature is always checked
against the key inside it.

**Provisioning a leaf**: an Ed25519 key made on the builder and readable by the shim alone; a certificate
from the bucket's CA with `BasicConstraints CA:FALSE` and `KeyUsage digitalSignature`; `notBefore`
backdated by a day, for readers whose clock lags; a validity of the rotation period plus the bucket's
object lifetime; a builder restart.

**Lifetimes.** Only the reader's clock bounds a stolen leaf key: a signing time in the payload would be
chosen by whoever holds the key, so none is recorded. A pointer is read for as long as the bucket keeps
it, so the leaf must outlive the last pointer signed under it by the object lifetime: that is the
validity rule above, and the builder refuses to sign once less than the object lifetime is left on its
certificate. A stolen leaf stays good until its `notAfter`; the answer is rotating the CA.

**Rotating the CA**: add the new certificate to every reader's `--authority`, switch the builder to a leaf
under it, remove the old certificate once nothing signed under it is left in the bucket. Readers accept
several authorities for this. An expired authority is dropped with a warning at startup; none left is a
startup error.

**Not here**: no revocation list and no transparency log, rotating the CA is the revocation; no
intermediate CAs, a leaf must be issued directly by a configured authority.

## Error reporting

Buck2 reports every kind of failure as a cache miss, and a failed upload as a warning it then ignores. So a
reader with the wrong authority, a bucket gone private and a builder whose uploads fail all look like a
slow build. The shim logs each event with its reason, and counts them by kind; `--status` prints the
counters of the shim serving a store, with its address, bucket, trust and store sizes, as JSON.

When a build is slower than it should be, read the counters of the machine's shim first:

- **`misses` high, `hits` near zero**: the bucket has nothing for this build. Check the builder's
  `published` counter, and that both build the same configuration.
- **`pointers refused`**: signatures do not check out. Check the reader's `--authority`, then the log for
  the key id and the reason.
- **`bundles refused`**: a bundle did not match its result. Read the log; the builder re-uploads that
  bundle on its next publish.
- **`bucket errors`**: the bucket answered badly or not at all. The log has the HTTP status: network,
  permissions, or the read token.
- **`bundles gone`**: pointers whose bundle has expired. Check the bucket's age rules if it keeps happening.
- **`incomplete`**: a result whose outputs could not all be had. Usually `bundles gone` in disguise.
- **`publish failures`** on a builder: uploads fail and Buck2 only warned. Check the write key and the log.

A shim that does not answer `--status` is not running; the build tool starts one on the next build.


## What Buck2 asks for

Nine RPCs, from its own client (`remote_execution/oss/re_grpc/src/client.rs`): `get_capabilities`,
`get_action_result`, `update_action_result`, `find_missing_blobs`, `batch_update_blobs`,
`batch_read_blobs`, and ByteStream `read`, `write`, `query_write_status`. No Execution service.

Only `get_action_result` and `update_action_result` reach the bucket. `find_missing_blobs`,
`batch_read_blobs` and ByteStream `read` are answered from the local store, ranges included; the bucket is
never asked whether an output exists.

`get_capabilities` is not optional: the client queries it unless a project says otherwise, and reads back
the batch size limit and the compressor list.

Two things about Buck2 that are easy to lose a day to:

- **`-c buck2_re_client.address=...` is ignored.** The address only reaches Buck2 through a config *file*
  read when a daemon starts, and the daemon has to be killed for a change to take effect. `tine buck`
  replaces the daemon whenever the address it wrote changes.
- **The isolation dir is part of every action digest**, because output paths reach the action's command and
  they read `buck-out/<isolation dir>/...`. The default isolation dir is literally `v2`. Two isolation dirs
  never share a cache entry.

Buck2 also stores one extra empty result per cache to check it may write at all
(`buck2_execute_impl/src/executors/empty_action_result.rs`), under a digest built from a compiled-in
command and the platform properties. One per platform.

## Running it

One shim per user, started by the build tool before a build if nothing is serving that user's store yet,
and exiting on its own after being idle for 15 minutes. The store, the status socket and the port are all
per user for the same reason: a second user's shim must not find the first's port taken and the first's
store locked. A CI runner that is new for every job either keeps the local store on a persistent
volume/directory, or pays the bundle fetches every time.
