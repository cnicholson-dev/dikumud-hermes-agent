# DikuMUD Compatibility Patches

Applied in numeric order with `patch -p1` from inside the `dm-dist-alfa`
source directory. All are build-portability fixes for a modern toolchain. None
changes game rules, world content, balance, or the command set.

Upstream base: commit `81b74dce0436b782d08b19064e32013c73525b45`.
Toolchain: GCC 12.2.0 on Debian 12 (bookworm), glibc 2.36.

The upstream makefile builds with `-Werror` and already disables a long list of
warning classes. Patches 0001 through 0004 address everything that stops the
build: four compile errors across four files, plus one link failure. With those
applied, `make` completes clean and produces `dmserver` and `delplay`.

Patches 0005, 0006 and 0007 are different in kind. They fix runtime defects
that only appear once the server is running: authentication silently rejecting
correct passwords, the game freezing when a client vanishes, and the
change-password menu refusing every correct retype. None was found by the
compiler; each was found by using the thing.

## 0001-posix-signal-mask-compat.patch

**Problem.** `comm.c` and `modify.c` use the 4.2BSD signal-mask API,
`sigmask()` and `sigsetmask()`, at 30 call sites. glibc still provides them but
marks them deprecated via `__glibc_macro_warning()`. GCC emits that diagnostic
with **no controlling `-W` option**, so it prints as bare `[-Werror]` and is
fatal.

**Why not just suppress it.** `-Wno-error=cpp`, `-Wno-error=pragmas`, and
`-Wno-cpp` were each tested against the full upstream flag set. None suppresses
it. Only a blanket `-Wno-error` would, which would disable the warning
discipline for every other file in the build.

**Fix.** Adds `sigcompat.h`, which undefines the deprecated macros and
reimplements `sigmask()` and `sigsetmask()` on top of POSIX `sigprocmask(2)`
with identical semantics: `sigsetmask()` replaces the blocked mask outright and
returns the previous mask. The header is included in the two files that need
it. No call site changes.

Only `sigsetmask` is reimplemented, because `sigblock` is not used anywhere in
the tree.

## 0002-act-wizard-init-newlevel.patch

**Problem.** In `do_advance()`, when the target is level 0 the code sets
`adv = 1` but never assigns `newlevel`. It is then read by the `sprintf()` that
builds the log line, so the log records whatever was on the stack.

This is a real latent bug, not just a warning. GCC's
`-Werror=maybe-uninitialized` is correct here.

**Fix.** Sets `newlevel = GET_LEVEL(victim) + adv` in that branch, which is the
level the character is actually advancing to, so the log line reports the truth.

`do_advance` is an implementor command and is unreachable for the mortal demo
character, but the file must still compile.

## 0003-act-informative-array-address-test.patch

**Problem.** `act.informative.c` tests `if ((d->host) && *(d->host))`, but
`host` is declared `char host[50]` in `structs.h`. The address of an array is
never NULL, so the first operand is always true. GCC rejects it under
`-Werror=address`.

**Fix.** Drops the always-true operand, leaving `if (*(d->host))`, which is the
emptiness test the code intended. Behavior is unchanged.

## 0004-db-single-definition-reset-q.patch

**Problem.** `db.h` line 150 ended the `reset_q_type` struct with `} reset_q;`,
which *defines* the variable rather than declaring it. Every translation unit
that includes `db.h` therefore emits its own definition. GCC 10 and later
default to `-fno-common`, so the linker rejects this:

```
ld: reception.o: multiple definition of `reset_q'; changes.o: first defined here
```

This is a link-time failure, so it only appears once every file compiles.

**Fix.** `db.h` now closes the struct with `};` and declares
`extern struct reset_q_type reset_q;`. The single definition moves to `db.c`,
alongside the other module globals. `db.c` is the only file that touches
`reset_q`, so nothing else is affected.

Rejected alternative: adding `-fcommon` to `CFLAGS`. It is a one-line change,
but it re-enables the pre-GCC-10 tentative-definition behavior for the whole
build and leaves the duplicate definitions in place. Declaring the variable
correctly fixes the actual defect instead of restoring the tolerance for it.

## 0005-init-crypt-data-struct.patch

**Problem.** `nanny()` in `interpreter.c` declared `struct crypt_data crypted;`
as an uninitialized stack local and passed it to all five `crypt_r()` call
sites. libxcrypt requires that structure to be cleared before first use. Left
as stack garbage, `crypt_r()` can return an incorrect hash, so a correct
password fails to authenticate.

The upstream repository already modernized `crypt()` to `crypt_r()` for
thread-safety, but did not add the zero-initialization that `crypt_r()`
requires.

**Symptom.** Login intermittently rejects the correct password with
"Wrong password." In this environment the failure tracked password length:
8-character passwords authenticated, 10-character ones did not. That
correlation is incidental. The real variable is the uninitialized structure,
and the observed behavior depends on whatever happened to be on the stack.

**How it was confirmed.** A debug build logged the comparison at the login
check:

```
DBG arglen=10 computed=BeJzwR54wTRHI stored=BeJzwR54wTRHI
```

The computed and stored hashes are identical, yet `strcmp()` reported a
mismatch and the server answered "Wrong password." Adding that single extra
`crypt_r()` call ahead of the real one was itself enough to make the same login
succeed, because the warm-up call left the structure in a usable state. That
is what identified the uninitialized struct as the cause rather than anything
about the password or the stored record.

Independently verified before the fix: the hashes written to the player file
were correct. Computing `crypt_r(<the passphrase>, <the character name>)` in
isolation produced exactly the hash stored for that character, so the defect was
in verification, not in storage.

**Fix.** Declares the structure as `struct crypt_data crypted = {0};`. Storage
class and lifetime are unchanged; the structure is simply cleared. One
declaration serves all five `crypt_r()` call sites, so this is the only change
needed.

The structure is 32,768 bytes, so this adds a 32KB clear per `nanny()` call.
`nanny()` only runs for input received while a descriptor is in a login or menu
state, never in the playing game loop, so the cost is irrelevant.

**Verified after the fix.** Four accounts with 8- and 10-character passwords
all authenticate:

```
Alpha  (pw 8 chars):  LOGIN OK
Beta   (pw 10 chars): LOGIN OK
Wren   (pw 10 chars): LOGIN OK
Zeroth (pw 8 chars):  LOGIN OK
```

**Note on password length.** Independently of this defect, `interpreter.c`
rejects any password longer than 10 characters at creation
(`strlen(arg) > 10`), and the classic DES hash these salts produce is
significant only to 8 characters. The demo credential must respect both limits.

## 0006-no-so-linger-blocking-close.patch

**Problem.** `init_socket()` set `SO_LINGER` on the listening socket with
`l_onoff = 1` and `l_linger = 1000`. `SO_LINGER` is measured in **seconds**, so
this asks `close()` to block for up to 1000 seconds while unsent data drains.

Accepted sockets inherit socket options from the listener. Verified directly
with a test program that creates a listener with the same option and reads it
back from an accepted descriptor:

```
listener   : l_onoff=1 l_linger=1000
ACCEPTED fd: l_onoff=1 l_linger=1000  -> INHERITED
```

`close_socket()` calls `close(d->descriptor)` as its first action. When a client
disappeared with output still queued, that `close()` blocked. Because the server
is a single-threaded `select` loop, the entire game froze: no accepts, no ticks,
no combat rounds, no output, and no logging, until TCP resolved the connection.

**Symptom.** The server stops responding while staying alive. Measured during a
freeze: `State: S`, `utime`/`stime` not advancing, CPU ~0%, one thread,
`SigBlk: 0`, listening socket still open with connections queued unaccepted.
Freezes of 26 and 37 seconds were recorded, and one recovery took about four
minutes.

**How it was found.** A debug build logged a heartbeat from the game loop every
40 ticks, which turns the freeze into a visible gap in the log. Correlating a
37-second gap with the surrounding lines showed the server entering
`close_socket` and not reaching its next log statement.

An earlier hypothesis blamed `save_char()` or `act()`, because the last line
before a freeze was always an EOF. Instrumenting both calls disproved it: they
completed normally 54 times under load. The next log line inside the freeze
window was `Losing player: (null).`, which is emitted from the branch *before*
those calls, placing the block in the preceding `close(d->descriptor)`.

**Fix.** Do not set `SO_LINGER` at all. `close()` then returns immediately and
the kernel completes the shutdown in the background, which is the correct
behavior for a server that must not block on any single peer.

**Verified.** A client that fills the server's send buffer and vanishes without
reading, repeated four times:

```
unfixed : probe responsive after 36.6s
fixed   : 0.7s, 0.6s, 0.6s, 0.6s
```

A 90-second concurrent load with abrupt disconnects produced zero heartbeat gaps
over 15 seconds on the fixed build.

**Note on the `(null)` name.** `Losing player: (null).` shows a character whose
name pointer was null reaching the disconnect path. That is a separate latent
defect, not caused by and not fixed by this patch, and is recorded in the Phase
1 gate report rather than patched speculatively.

## 0007-fix-change-password-truncation.patch

**Problem.** Option 4 on the account menu, "Change password", cannot succeed.
It answers `Passwords don't match.` for every password, every time, for every
player. Found in Phase 8 while trying to exercise credential rotation.

The change-password path and the character-creation path are identical except
for one line:

```c
case CON_PWDGET:                     /* creation, works */
    strcpy(d->pwd, crypt_r(arg, d->character->player.name, &crypted));

case CON_PWDNEW:                     /* change password, broken */
    strcpy(d->pwd, crypt_r(arg, d->character->player.name, &crypted));
    *(d->pwd + 10) = '\0';           /* <- the only difference */
```

Both then verify the retyped password the same way:

```c
    if (strcmp(crypt_r(arg, d->pwd, &crypted), d->pwd))
```

That comparison is the standard crypt idiom: hash the input using the stored
hash as the salt, because DES crypt reads the salt from the first two
characters. It works in creation, where `d->pwd` holds the whole hash. In the
change path `d->pwd` has been truncated to ten characters, so a full
thirteen-character hash is compared against a ten-character prefix and
`strcmp` can never return zero.

The truncation is not protecting a buffer. `structs.h` declares
`char pwd[CRYPT_OUTPUT_SIZE+1]` in both the descriptor and the character, sized
for the whole hash. The line is a leftover from when that field was `char[11]`,
and the surrounding code was modernised around it.

**Fix.** Delete the truncation, making the change-password path identical to
the creation path that every character on the server already went through.

**Verified.** Before: `Passwords don't match.` on every attempt, tested with
8- and 10-character passwords, LF and CRLF line endings, and pauses between
sends. After: the password changes, the server answers `Done. You must enter
the game to make the change final`, the new password is accepted, the old one
is rejected, and the agent logs in with the rotated credential.

**Upstream.** Present in `81b74dce`, unreported. The repository has one open
issue, #5, about `crypt_r` and DES availability under modern glibc, which is a
different defect and the subject of patch 0005.
