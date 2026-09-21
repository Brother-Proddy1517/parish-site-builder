# SITE_BUILD.PY(1) — Parish Site Builder Manual

## NAME

`site_build.py` — create, deploy, and manage nginx-hosted parish websites

## SYNOPSIS

```
site_build.py create <name> [--domain DOMAIN] [--git URL] [--branch BRANCH]
                             [--ssl --email EMAIL]
site_build.py list
site_build.py status [name]
site_build.py enable <name>
site_build.py disable <name>
site_build.py remove <name> [--keep-files] [--yes]
site_build.py update <name>
site_build.py enable-ssl <name> --email EMAIL
site_build.py doctor
```

Every command that changes the filesystem or nginx config must be run as
root (`sudo`), since it writes to `/srv/www` and `/etc/nginx`, and reloads
or restarts the nginx service.

## DESCRIPTION

`site_build.py` manages one nginx-hosted site per invocation of `create`.
Each site gets its own directory under `/srv/www`, its own nginx server
block, and — optionally — its own linked git repository that can be
re-deployed later with `update` instead of `scp`-ing files by hand.

The tool is built around one principle above all others: **if something
is wrong, nothing is changed.** Every command that creates or modifies
state runs a full set of checks first. If any check fails, the command
prints exactly what's wrong and exits without touching the filesystem or
nginx. Where a later step can still fail after changes have begun (for
example `nginx -t` failing after a config file has already been written),
the tool rolls back what it already did, rather than leaving nginx in a
half-configured state.

## ON-DISK LAYOUT

Every site lives entirely under one directory:

```
/srv/www/<name>/                    site base
/srv/www/<name>/.parish-site.json   metadata: domain, linked git repo/branch
/srv/www/<name>/index.html          served content, if NOT git-linked
/srv/www/<name>/repo/               git working tree, if git-linked
/srv/www/<name>/repo/public/        served content, if git-linked
```

**Why `repo/public/` and not just `repo/`:** nginx does not block
dotfiles by default. If the git working tree itself were nginx's web
root, `repo/.git/config` (and anything else in the repository) would be
fetchable by anyone over plain HTTP — `curl https://yoursite.org/.git/config`
would work. Keeping the checkout in `repo/` and pointing nginx only at
`repo/public/` makes that impossible: nginx's root never contains `.git`
at all.

nginx configuration lives in the usual Debian/Ubuntu locations:

```
/etc/nginx/sites-available/<name>   the generated server block
/etc/nginx/sites-enabled/<name>     symlink to sites-available/<name>, if enabled
```

A site with a file in `sites-available` but no symlink in `sites-enabled`
is **disabled**: its config exists but nginx isn't serving it. This is
the `disable`/`enable` distinction.

## COMMANDS

### `create <name> [--domain DOMAIN] [--git URL] [--branch BRANCH]`

Creates a new site: the directory, an nginx config, and (if content isn't
git-linked) a placeholder `index.html` so the site isn't a bare 404 while
you're setting things up.

**Arguments:**

- `name` — required. Used as the directory name under `/srv/www`, the
  nginx config filename, and (if `--domain` is omitted) the domain. Must
  start with a letter or digit and contain only letters, digits, hyphens,
  and underscores (max 63 characters) — this keeps it safe as both a
  filesystem path component and an nginx config filename.
- `--domain` — the value used in nginx's `server_name` directive. Defaults
  to `name` if omitted, which is convenient for local/VM testing without
  a real DNS name, but you'll want a real domain before going live.
- `--git` — a git URL to clone as the site's initial content, instead of
  the placeholder `index.html`. See GIT DEPLOYMENT below for the expected
  URL format and required SSH setup.
- `--branch` — which branch to clone and later track with `update`.
  Defaults to `main`. Only meaningful when `--git` is given.

**What it does, in order:**

1. Runs every pre-flight check (see PRE-FLIGHT CHECKS below). If any
   fails, prints the full list and exits — nothing is created.
2. Creates `/srv/www/<name>`.
3. Writes `.parish-site.json` with the site's name, domain, and (if
   applicable) linked git repo/branch.
4. Either clones the git repo into `repo/` (if `--git` given) or writes a
   placeholder `index.html` directly in the site directory.
5. Writes the nginx config, pointing `root` at `repo/public/` (git-linked)
   or the site directory itself (not git-linked).
6. Symlinks the config into `sites-enabled`.
7. Runs `nginx -t`. If it fails, rolls back the config and symlink, and
   nginx is never reloaded — no other site is affected.
8. Reloads nginx.
9. Probes the site over HTTP (see VERIFICATION below) to confirm it's
   actually being served, not just configured.
10. Prints a summary of everything created, with full paths.

**If the git clone fails** (bad deploy key, repo doesn't exist, network
issue), the entire site directory — including the metadata file that was
already written — is removed. nginx is never touched.

**Examples:**

```bash
sudo python3 site_build.py create messiah --domain messiah.example.org

sudo python3 site_build.py create messiah \
    --domain messiah.example.org \
    --git git@github.com:yourorg/messiah-site.git \
    --branch main
```

### `list`

Prints a table of every site nginx knows about (i.e. every file in
`sites-available`, excluding the Debian default site), showing:

```
SITE                DOMAIN                        ENABLED   GIT
messiah             messiah.example.org           yes       yes
stmarks             stmarks.example.org           no        no
```

Domain and git-linked status are read from each site's `.parish-site.json`
when present. Sites created before v0.4 (no metadata file) still show up
— domain is parsed directly from the nginx config as a fallback.

This command makes no HTTP requests and is safe to run frequently.

### `status [name]`

With a site name, prints detailed status for just that site:

```
Site:    messiah
Domain:  messiah.example.org
Enabled: yes
HTTP:    OK
Git:     git@github.com:yourorg/messiah-site.git (main) @ a1b2c3d
```

`HTTP` reflects a live probe (see VERIFICATION below) — `OK`, or
`FAIL - <reason>` if the site isn't actually reachable despite being
enabled. If the site is disabled, HTTP is not checked.

`Git` shows the linked repo, branch, and current commit (via
`git rev-parse --short HEAD` in `repo/`), or `not linked` if the site has
no git repo configured.

Without a name, prints a summary table of every site with a live HTTP
check for each enabled one:

```
SITE                HTTP    GIT
messiah             OK      linked
stmarks              off    -
```

Since this checks every enabled site over HTTP, it's slower than `list`
on a box with many sites — use `list` for a quick inventory, `status` when
you actually want to know if things are working right now.

### `enable <name>`

Re-adds the `sites-enabled` symlink for a previously-disabled site, runs
`nginx -t`, and reloads. If the site is already enabled, this is a no-op
that exits successfully. If the config was already removed (e.g. after
`remove --keep-files` was never followed by a fresh `create`), this fails
with `No such site`.

### `disable <name>`

Removes the `sites-enabled` symlink (leaving the config in
`sites-available` and the site directory untouched) and reloads nginx.
Use this to temporarily take a site offline — for maintenance, a billing
lapse, whatever — without losing any configuration or content. Already
disabled is a no-op success.

### `remove <name> [--keep-files] [--yes]`

Deletes a site. By default, removes the `sites-enabled` symlink, the
`sites-available` config, **and** the entire `/srv/www/<name>` directory
(including any git checkout and the metadata file).

- `--keep-files` — remove the nginx config and symlink, but leave the
  site directory on disk. Useful if you want to stop serving a site
  without losing its content.
- `--yes` — skip the "are you sure?" confirmation prompt. Without it,
  `remove` prints exactly what will be deleted and waits for confirmation
  before doing anything.

This is the one command that destroys data, so by default it always asks
first.

### `update <name>`

Pulls the latest version of a git-linked site's content. Requires that
the site was created with `--git` (or otherwise has a valid
`.parish-site.json` with git info and a real checkout in `repo/`).

Runs, in `repo/`:

```bash
git fetch origin
git reset --hard origin/<branch>
git clean -fd
```

This always makes the working tree match the remote exactly — any local
edits made directly on the server, and any untracked files, are
discarded. This is a deliberate choice: for a hosting tool, "always
matches the repo" is a safer default than trying to reason about merge
conflicts on a production box. **If you need to preserve server-only
files, don't put them in `repo/` — they will be deleted.**

No nginx reload is needed or performed — static files take effect the
moment they're written to disk. `update` finishes by probing the site
over HTTP the same way `create` does, and will offer to restart nginx if
something looks wrong (though this is unlikely for a pure content
update).

If the site has no git repo configured, or `repo/.git` is missing or
corrupted, `update` fails immediately with a clear message rather than
attempting anything.

### `enable-ssl <name> --email EMAIL`

Issues a Let's Encrypt certificate for a site that already exists, and
switches it to HTTPS. This is the standalone path — use it once a site's
DNS has actually propagated, rather than gambling on `--ssl` at creation
time before you're sure the domain resolves.

**Requires**, checked before anything happens:

- The site exists and is currently **enabled** (certbot's webroot
  challenge needs the site actively serving HTTP to place the challenge
  file where Let's Encrypt's validators can fetch it).
- The site doesn't already have SSL configured.
- `certbot` is installed, the email is valid, the domain contains a dot
  (Let's Encrypt won't issue for bare local names), and no certificate
  already exists at `/etc/letsencrypt/live/<domain>` for a different
  reason.

**What it does:**

1. Runs `certbot certonly --webroot -w <current-nginx-root> -d <domain>`.
2. Rewrites the nginx config: HTTP now redirects to HTTPS, HTTPS serves
   the site with `ssl_certificate`/`ssl_certificate_key` pointing at the
   new cert.
3. Runs `nginx -t`. If it fails, the **config is restored to its exact
   previous content** — the site keeps serving over HTTP, untouched,
   exactly as if `enable-ssl` had never been run.
4. Reloads nginx.
5. Probes the site over real HTTPS (not loopback — see VERIFICATION
   below) to confirm the cert and TLS handshake actually work.

**If certificate issuance itself fails** (DNS not propagated yet, rate
limited, etc.), nothing has touched nginx yet — the site is completely
unaffected, exactly as it was before the command ran. This is a
deliberately different failure mode from `create --ssl`: an *existing*
site should never be put at risk by a failed SSL attempt.

### `create ... --ssl --email EMAIL`

A shortcut for doing SSL setup in the same breath as `create`, for cases
where you already know the domain resolves (e.g. testing against an
`sslip.io` address, or a domain you've already pointed at the VPS).

**Important difference from `enable-ssl`:** if certificate issuance
fails here, **the entire site is rolled back** — nginx config, symlink,
and the whole site directory — exactly like any other `create` failure
(e.g. a failed `nginx -t`). This is a deliberate choice: if you asked for
`--ssl` and didn't get it, you likely don't want an HTTP-only site
silently left behind that you didn't ask for. If you're not sure DNS is
ready yet, create the site plain first and run `enable-ssl` once you've
confirmed it resolves — that path leaves the site alone on failure.

## SSL / CERTIFICATES

**Why `certonly --webroot`, not the `--nginx` plugin:** certbot's
`--nginx` plugin edits your nginx config file directly to add the SSL
block. That conflicts with this tool's whole design — we own and
regenerate every config from our own template, and if certbot edited it
behind our back, the next regeneration would clobber those changes and
our rollback guarantees would no longer hold. `certonly --webroot` only
obtains the certificate files (`/etc/letsencrypt/live/<domain>/
fullchain.pem` and `privkey.pem`); we write the SSL server block
ourselves.

**The webroot challenge requires the site to already be live over HTTP.**
Certbot proves domain ownership by writing a file under
`<current-nginx-root>/.well-known/acme-challenge/` and having Let's
Encrypt's servers fetch it over plain HTTP on port 80. This means:

- `enable-ssl` requires the site to be enabled first.
- `create --ssl` issues the cert *after* the plain-HTTP site has already
  been created, enabled, and verified reachable — not before.

**Renewal is not reimplemented here.** Installing certbot via `apt` also
installs a systemd timer that checks for and renews expiring
certificates automatically, system-wide. This tool's job stops at
issuance and reporting; re-running renewal logic ourselves would
duplicate something certbot already does reliably, with real failure
modes (rate limits, etc.) better left to the well-tested original.

**Expiry is read from the actual certificate file**, not trusted from
certbot's internal state — `openssl x509 -enddate -noout` against
`/etc/letsencrypt/live/<domain>/fullchain.pem`. This is the same
"verify reality" principle behind `verify_site_serving`: what matters is
what's actually true on disk and over the wire, not what a tool's exit
code implied.

**HTTPS verification** (after `create --ssl` or `enable-ssl` succeeds)
makes a real TLS connection to the domain over the public internet —
unlike the HTTP verification step, this can't use loopback, since a
successful TLS handshake against the real hostname is itself the
meaningful signal: it proves the certificate matches the domain and
chains to a trusted CA, not merely that a file exists somewhere.

**A real constraint worth knowing before you test this:** SSL genuinely
cannot be exercised against a fake local domain the way HTTP could be
tested with a hosts-file trick. Let's Encrypt needs to reach your server
over the real internet on port 80. For testing without owning a domain,
a free service like `sslip.io` (`<your-vps-ip>.sslip.io` resolves
automatically to that IP) works well and exercises the exact same flow a
real parish domain would.

## PRE-FLIGHT CHECKS

`create` runs every one of these before making any change. Printed as:

```
Preflight checks:
  [PASS] Valid site name
  [PASS] Valid domain
  [PASS] Valid git URL              (only if --git given)
  [PASS] git installed: /usr/bin/git (only if --git given)
  [PASS] nginx installed: /usr/sbin/nginx
  [PASS] nginx running
  [PASS] Site directory available
  [PASS] Nginx configuration available
  [PASS] Domain not already in use
```

| Check | What it catches |
|---|---|
| Valid site name | Names that would be unsafe as a directory or nginx config filename (slashes, spaces, leading hyphens, over 63 chars) |
| Valid domain | Malformed hostnames |
| Valid git URL *(if `--git`)* | Obvious typos — must start with `https://`, `git@`, or `ssh://` |
| git installed *(if `--git`)* | Missing `git` binary |
| certbot installed *(if `--ssl`)* | Missing `certbot` binary |
| Valid email *(if `--ssl`)* | Malformed contact email for Let's Encrypt |
| Domain looks like a real hostname *(if `--ssl`)* | Bare local names (no dot) that Let's Encrypt can never issue for |
| No existing certificate for domain *(if `--ssl`)* | Avoids collision-suffix confusion from certbot if a cert directory already exists |
| nginx installed | Missing `nginx` binary entirely |
| nginx running | `nginx` installed but the service isn't active |
| Site directory available | `/srv/www/<name>` already exists |
| Nginx configuration available | A config or enabled symlink for `<name>` already exists |
| Domain not already in use | A **different** site's config already claims this exact domain as `server_name` |

## `doctor`

Checks that the whole machine is ready to host sites — not tied to any
one site. Useful right after setting up a fresh VPS, before you ever run
`create`.

```
System check:
  [PASS] Running as root
  [PASS] nginx installed: /usr/sbin/nginx
  [PASS] nginx running
  [PASS] nginx enabled on boot
  [PASS] git installed: /usr/bin/git
  [PASS] openssl installed: /usr/bin/openssl
  [PASS] certbot installed: /usr/bin/certbot
  [PASS] certbot renewal timer active
  [PASS] /srv/www exists
  [PASS] /etc/nginx/sites-available exists
  [PASS] /etc/nginx/sites-enabled exists
  [PASS] nginx listening on port 80 (local check)
  [PASS] nginx listening on port 443 (local check)
  [WARN] ufw allows port 443: Run: sudo ufw allow 443/tcp

System looks ready. (WARNs are advisory, not blocking.)
```

Unlike pre-flight checks, `doctor` never changes anything — it only
reports. Checks are `PASS`, `WARN`, or `FAIL`: a missing *required* piece
(nginx, `/srv/www`, root privileges) is `FAIL`; a missing *optional*
piece (`git`, `certbot` — only needed if you're using those features) is
`WARN`. Only `FAIL`s affect the exit code.

The port-listening check only confirms nginx is bound **locally** — it
cannot confirm the port is reachable from the internet (that depends on
your VPS provider's network-level firewall/security groups, which this
tool has no visibility into). If `ufw` isn't installed, its checks are
skipped silently rather than reported as failures — many VPS providers
firewall at the network level instead of locally.

If any check fails, the full list is printed with `[FAIL]` and a specific
reason, followed by `No changes were made.` — and the command exits
non-zero.

## VERIFICATION

After `create` reloads nginx (and after `update` finishes), the tool
makes an actual HTTP request to the site — over both IPv4 (`127.0.0.1`)
and IPv6 (`::1`) loopback, with the correct `Host` header — and checks
whether the response looks like the Debian/Ubuntu default nginx welcome
page instead of the site's real content.

This exists because of a real, reproducible nginx quirk: `systemctl
reload nginx` re-reads config files but doesn't always rebind newly added
`listen` sockets, particularly the IPv6 one for a brand-new site. A config
that passes `nginx -t` and reloads without error can still silently fall
through to the default site on one address family. A full `systemctl
restart nginx` always fixes this, but briefly disconnects every site on
the box, not just the new one — so the tool checks first, and only
restarts if you confirm:

```
Verifying site:
  [FAIL] messiah.example.org is not being served correctly
         IPv6: still serving the default nginx page, not messiah.example.org

  This usually happens when 'reload' doesn't rebind a newly added
  listen socket. A full restart fixes it, but briefly disconnects
  every site on this server, not just this one.
Restart nginx now to fix this? [y/N]:
```

## GIT DEPLOYMENT

Git access uses **SSH, with root's SSH key** — since the tool is always
run under `sudo`, root is what actually invokes `git`. This means no
tokens are embedded in URLs or stored in `.git/config`; the credential is
a key file on disk instead.

**One-time setup per VPS:**

```bash
sudo ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ""
sudo cat /root/.ssh/id_ed25519.pub
```

Add the printed public key as a **Deploy key** on each repo you want to
deploy from — on GitHub: repo → Settings → Deploy keys → Add deploy key.
Read-only access is sufficient; the tool never pushes.

**First-connection host key prompt:** normally, the first time SSH
connects to a new host, it asks
`Are you sure you want to continue connecting (yes/no)?` and waits for
input — which would hang a non-interactive deploy. To avoid this, every
git command the tool runs sets:

```
GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new"
```

This accepts a new host's key automatically on first contact (and still
verifies it on every subsequent connection, same as normal SSH behavior)
— it does not disable host key checking altogether.

**Repo URL format:** use the SSH form, e.g.
`git@github.com:yourorg/messiah-site.git`. HTTPS URLs are also accepted
by the `--git` validity check, but SSH is what the deploy-key setup above
is built for.

## EXIT STATUS

`0` on success. Non-zero on any failure — a failed pre-flight check, a
failed nginx config test, a failed git operation, or (for `remove`)
declining the confirmation prompt. Scripts wrapping this tool should
check the exit code rather than parsing output.

## SEE ALSO

`nginx(8)`, `git(1)`, `systemctl(1)`, `ssh-keygen(1)`
