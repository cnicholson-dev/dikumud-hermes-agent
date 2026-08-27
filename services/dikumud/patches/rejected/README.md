# Rejected patches

Kept for the record, not applied by the Dockerfile.

## 0006-recompute-maxdesc-on-close.patch

Written while investigating a server that stopped accepting connections during
Phase 1 verification, then withdrawn because it fixes nothing demonstrable.

`close_socket()` contains `if (d->descriptor == maxdesc) --maxdesc;`, which
looks like careless bookkeeping. On analysis it is safe:

- `new_descriptor()` raises the bound only via `if (desc > maxdesc) maxdesc = desc;`,
  so `maxdesc` is always at least the highest open client descriptor.
- Two descriptors cannot share an fd, so when the one equal to `maxdesc`
  closes, every remaining descriptor is at most `maxdesc - 1`. Decrementing by
  one therefore still covers all of them.
- It cannot fall below the listening socket, because the listener's fd is never
  a member of `descriptor_list`, so no close can ever match it.

The patch replaced the decrement with a full recompute against a new
`mother_desc` global. Strictly more defensive, but it corrects no observable
behavior, and changes to the upstream source are kept minimal. Applying an
unnecessary patch to upstream LGPL source is not free: it is one more thing to
carry and re-verify on every upstream change.

The unexplained server wedge this patch was written for remains open, and is
not attributed to this code.
