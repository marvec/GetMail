# GetMail

Download all emails (including attachments) from all folders on an IMAP server into a single `tar.zst` archive.

## Requirements

- Python 3.6+
- `zstd` or GNU tar with zstd support

```sh
# macOS
brew install zstd
# or
brew install gnu-tar

# Debian/Ubuntu
apt install zstd
```

## Usage

```sh
python3 getmail.py -H imap.example.com -u user@example.com
```

The tool prompts for any missing values (host, username, password).

### Options

| Flag | Env var | Default | Description |
|------|---------|---------|-------------|
| `-H`, `--host` | `IMAP_HOST` | _(prompted)_ | IMAP server host |
| `-P`, `--port` | `IMAP_PORT` | 993 (ssl) / 143 | IMAP server port |
| `-s`, `--security` | `IMAP_SECURITY` | `ssl` | `ssl`, `starttls`, or `none` |
| `-u`, `--user` | `IMAP_USER` | _(prompted)_ | Username |
| `-p`, `--password` | `IMAP_PASSWORD` | _(prompted)_ | Password |
| `--since` | | _(none)_ | Only emails on or after this date (inclusive, YYYY-MM-DD) |
| `--until` | | _(none)_ | Only emails before this date (exclusive, YYYY-MM-DD) |
| `--incremental` | | _(none)_ | Path to previous archive for incremental backup |
| `-o`, `--output` | | `emails.tar.zst` | Output file path |
| `-t`, `--threads` | | `4` | Parallel download threads |

### Password input

The password can be provided via flag, environment variable, or stdin:

```sh
# Flag
python3 getmail.py -H imap.example.com -u user -p secret

# Environment variable
IMAP_PASSWORD=secret python3 getmail.py -H imap.example.com -u user

# Pipe
echo 'secret' | python3 getmail.py -H imap.example.com -u user

# Interactive prompt (when stdin is a terminal)
python3 getmail.py -H imap.example.com -u user
Password: 
```

### Date filtering

Download only emails within a date range:

```sh
# All emails from 2024 onwards
python3 getmail.py -H imap.example.com -u user --since 2024-01-01

# All emails before 2024
python3 getmail.py -H imap.example.com -u user --until 2024-01-01

# First half of 2024 only
python3 getmail.py -H imap.example.com -u user --since 2024-01-01 --until 2024-07-01
```

`--since` is inclusive (on or after), `--until` is exclusive (strictly before). Both are optional and can be used independently or together.

### Incremental backups

Each archive contains a `_manifest.json` (as the first tar entry) that records the IMAP UIDVALIDITY and highest downloaded UID per folder. Use `--incremental` to point at a previous archive and download only new emails:

```sh
# First full backup
python3 getmail.py -H imap.example.com -u user -o backup-001.tar.zst

# Later: only new emails since the last backup
python3 getmail.py -H imap.example.com -u user --incremental backup-001.tar.zst -o backup-002.tar.zst

# Chain further incrementals
python3 getmail.py -H imap.example.com -u user --incremental backup-002.tar.zst -o backup-003.tar.zst
```

If a folder's UIDVALIDITY has changed (e.g. server rebuilt the folder), that folder is fully re-downloaded regardless.

To restore, extract all archives in order — later ones contain only the newer emails.

You can inspect the manifest without extracting the whole archive:

```sh
tar --zstd -xf backup-001.tar.zst -O _manifest.json | python3 -m json.tool
```

### Full example with environment variables

```sh
export IMAP_HOST=imap.example.com
export IMAP_USER=user@example.com
export IMAP_PASSWORD=secret
export IMAP_SECURITY=ssl

python3 getmail.py -o backup.tar.zst -t 8
```

## Output

The archive contains `.eml` files organized by folder:

```
INBOX/
  1.eml
  2.eml
Sent/
  1.eml
Drafts/
  3.eml
```

Each `.eml` file is a complete RFC 822 message with all headers, body, and attachments.

To extract:

```sh
tar --zstd -xf emails.tar.zst
# or
zstd -d emails.tar.zst --stdout | tar xf -
```

## Notes

- Emails are opened read-only — no flags are modified on the server.
- Many IMAP servers limit concurrent connections per user (often 10–20). Keep `--threads` within your server's limit.
- The tool uses IMAP UIDs as filenames, which are unique within each folder.
