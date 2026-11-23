#!/usr/bin/env python3
"""Download all emails from an IMAP server into a tar.zst archive."""

import argparse
import datetime
import getpass
import imaplib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Allow emails up to ~100 MB (default is 1 MB in modern Python)
imaplib._MAXLINE = 100_000_000

log = logging.getLogger("getmail")


def imap_utf7_decode(data):
    """Decode IMAP modified UTF-7 folder names to Unicode."""
    if isinstance(data, bytes):
        data = data.decode("ascii", errors="replace")
    result = []
    i = 0
    while i < len(data):
        if data[i] == "&":
            try:
                end = data.index("-", i + 1)
            except ValueError:
                result.append(data[i:])
                break
            if end == i + 1:
                result.append("&")
            else:
                b64 = data[i + 1 : end].replace(",", "/")
                try:
                    result.append(("+" + b64 + "-").encode("ascii").decode("utf-7"))
                except (UnicodeDecodeError, UnicodeEncodeError):
                    result.append(data[i : end + 1])
            i = end + 1
        else:
            result.append(data[i])
            i += 1
    return "".join(result)


def connect(host, port, security):
    """Create an IMAP connection with the given security method."""
    if security == "ssl":
        return imaplib.IMAP4_SSL(host, port)
    conn = imaplib.IMAP4(host, port)
    if security == "starttls":
        conn.starttls()
    return conn


def quote_folder(name):
    """Quote an IMAP folder name for use in commands."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


_FOLDER_RE = re.compile(rb"\(([^)]*)\)\s+\"([^\"]*)\"\s+(.*)")


def list_folders(conn):
    """Return list of (raw_name, display_name) for all selectable folders."""
    status, data = conn.list()
    if status != "OK":
        return []
    folders = []
    for item in data:
        if not isinstance(item, bytes):
            continue
        m = _FOLDER_RE.match(item)
        if not m:
            continue
        flags, _delim, raw_name = m.group(1), m.group(2), m.group(3).strip()
        if raw_name.startswith(b'"') and raw_name.endswith(b'"'):
            raw_name = raw_name[1:-1]
        if b"\\NOSELECT" in flags.upper():
            continue
        raw_str = raw_name.decode("ascii", errors="replace")
        display = imap_utf7_decode(raw_name)
        folders.append((raw_str, display))
    return folders


class ProgressBar:
    """Simple thread-safe progress bar (no external dependencies)."""

    def __init__(self, total, desc="Progress"):
        self.total = total
        self.desc = desc
        self.current = 0
        self.errors = 0
        self.lock = threading.Lock()
        self.start_time = time.monotonic()
        self._draw()

    def update(self, error=False):
        with self.lock:
            self.current += 1
            if error:
                self.errors += 1
            self._draw()

    def _draw(self):
        if self.total == 0:
            return
        pct = self.current / self.total
        width = 30
        filled = int(width * pct)
        bar = "\u2588" * filled + "\u2591" * (width - filled)
        elapsed = time.monotonic() - self.start_time
        rate = self.current / elapsed if elapsed > 0 else 0
        eta = (self.total - self.current) / rate if rate > 0 else 0
        err_str = f" [{self.errors} err]" if self.errors else ""
        sys.stderr.write(
            f"\r{self.desc}: [{bar}] {self.current}/{self.total}"
            f" ({pct:.0%}) {rate:.1f} msg/s ETA {eta:.0f}s{err_str}  "
        )
        sys.stderr.flush()

    def finish(self):
        self._draw()
        sys.stderr.write("\n")
        sys.stderr.flush()


_local = threading.local()


def _ensure_conn(host, port, security, user, password):
    """Get or (re)create a thread-local IMAP connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = connect(host, port, security)
        _local.conn.login(user, password)
        _local.folder = None
    return _local.conn


def _reset_conn():
    """Discard the thread-local IMAP connection so the next call reconnects."""
    old = getattr(_local, "conn", None)
    if old is not None:
        try:
            old.logout()
        except Exception:
            pass
    _local.conn = None
    _local.folder = None


def download_one(folder_raw, uid, host, port, security, user, password, tmpdir):
    """Download a single email to tmpdir. Returns True on success."""
    for attempt in range(2):
        try:
            conn = _ensure_conn(host, port, security, user, password)
            if _local.folder != folder_raw:
                conn.select(quote_folder(folder_raw), readonly=True)
                _local.folder = folder_raw

            status, data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not data or data[0] is None:
                return False
            raw = data[0][1]
            safe_folder = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", folder_raw)
            folder_dir = os.path.join(tmpdir, safe_folder)
            os.makedirs(folder_dir, exist_ok=True)
            uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
            path = os.path.join(folder_dir, f"{uid_str}.eml")
            with open(path, "wb") as f:
                f.write(raw)
            return True
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, EOFError) as e:
            log.warning(
                "Attempt %d failed for UID %s in %s: %s", attempt + 1, uid, folder_raw, e
            )
            _reset_conn()
        except Exception as e:
            log.warning("Failed to fetch UID %s in %s: %s", uid, folder_raw, e)
            _reset_conn()
            return False
    return False


def _tar_supports(tar_path, *flags):
    """Check if a tar binary supports the given flags."""
    try:
        result = subprocess.run(
            [tar_path, *flags, "-cf", os.devnull, "--files-from", os.devnull],
            capture_output=True,
        )
        return result.returncode == 0
    except OSError:
        return False


def find_tar_cmd():
    """Find a tar command that supports zstd compression."""
    for cmd in ("gtar", "tar"):
        path = shutil.which(cmd)
        if not path:
            continue
        if not _tar_supports(path, "--zstd"):
            continue
        flags = [path, "--zstd"]
        # Suppress macOS ._AppleDouble / xattr entries
        if _tar_supports(path, "--zstd", "--no-mac-metadata"):
            flags.append("--no-mac-metadata")
        if _tar_supports(path, "--zstd", "--no-xattrs"):
            flags.append("--no-xattrs")
        return flags
    # Fall back to piping through zstd
    zstd = shutil.which("zstd")
    tar = shutil.which("tar")
    if zstd and tar:
        return None  # signal to use pipe mode
    sys.exit(
        "Error: no suitable tar+zstd found.\n"
        "Install GNU tar (brew install gnu-tar) or zstd (brew install zstd)."
    )


def _build_filelist(tmpdir):
    """Build a file list with _manifest.json first, written to a temp file.

    Returns the path to the file list.  The caller is responsible for cleanup
    (it lives inside tmpdir so it is removed together with it).
    """
    filelist_path = os.path.join(tmpdir, ".filelist")
    has_manifest = os.path.isfile(os.path.join(tmpdir, MANIFEST_NAME))
    skip = {MANIFEST_NAME, ".filelist"}
    entries = []
    if has_manifest:
        entries.append(MANIFEST_NAME)
    for dirpath, dirnames, filenames in os.walk(tmpdir):
        rel_dir = os.path.relpath(dirpath, tmpdir)
        if rel_dir == ".":
            filenames = [f for f in filenames if f not in skip]
        else:
            entries.append(os.path.join(".", rel_dir))
        for fname in sorted(filenames):
            entries.append(
                os.path.join(".", rel_dir, fname) if rel_dir != "." else fname
            )
    with open(filelist_path, "w") as f:
        f.write("\n".join(entries) + "\n")
    return filelist_path


def compress(tmpdir, output):
    """Compress tmpdir contents into a tar.zst archive.

    _manifest.json is placed first in the archive so it can be extracted
    quickly without decompressing the entire file.
    """
    abs_output = os.path.abspath(output)
    filelist = _build_filelist(tmpdir)
    tar_zstd = find_tar_cmd()

    if tar_zstd:
        cmd = list(tar_zstd) + [
            "-cf", abs_output, "-C", tmpdir, "--no-recursion", "-T", filelist,
        ]
        subprocess.run(cmd, check=True)
    else:
        # Pipe: tar | zstd > output
        # COPYFILE_DISABLE tells macOS BSD tar to skip ._AppleDouble files
        env = {**os.environ, "COPYFILE_DISABLE": "1"}
        tar_args = [
            "tar", "-cf", "-", "-C", tmpdir, "--no-recursion", "-T", filelist,
        ]
        with open(abs_output, "wb") as out_f:
            tar_proc = subprocess.Popen(
                tar_args, stdout=subprocess.PIPE, env=env,
            )
            zstd_proc = subprocess.Popen(
                ["zstd", "-q"],
                stdin=tar_proc.stdout,
                stdout=out_f,
            )
            tar_proc.stdout.close()  # let tar get SIGPIPE if zstd exits
            zstd_proc.wait()
            tar_proc.wait()
        if tar_proc.returncode != 0 or zstd_proc.returncode != 0:
            sys.exit("Error: tar/zstd compression failed.")


def parse_args():
    p = argparse.ArgumentParser(
        description="Download all emails from an IMAP server into a tar.zst archive.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s -H imap.example.com -u user@example.com
  %(prog)s -H imap.example.com -u user -s starttls -P 143
  echo 'secret' | %(prog)s -H imap.example.com -u user
  %(prog)s -H imap.example.com -u user --since 2024-01-01
  %(prog)s -H imap.example.com -u user --since 2024-01-01 --until 2024-07-01
  IMAP_HOST=imap.example.com IMAP_USER=user IMAP_PASSWORD=secret %(prog)s

Note: many IMAP servers limit concurrent connections per user (often 10-20).
Keep --threads within your server's limit.""",
    )
    p.add_argument(
        "-H", "--host",
        default=os.environ.get("IMAP_HOST"),
        help="IMAP server host (env: IMAP_HOST)",
    )
    p.add_argument(
        "-P", "--port",
        type=int,
        default=None,
        help="IMAP server port (env: IMAP_PORT, default: 993 for ssl, 143 otherwise)",
    )
    p.add_argument(
        "-s", "--security",
        default=os.environ.get("IMAP_SECURITY", "ssl"),
        choices=["ssl", "starttls", "none"],
        help="Connection security method (env: IMAP_SECURITY, default: ssl)",
    )
    p.add_argument(
        "-u", "--user",
        default=os.environ.get("IMAP_USER"),
        help="Username (env: IMAP_USER)",
    )
    p.add_argument(
        "-p", "--password",
        default=os.environ.get("IMAP_PASSWORD"),
        help="Password (env: IMAP_PASSWORD, or prompted on stdin)",
    )
    p.add_argument(
        "--since",
        default=None,
        help="Only download emails on or after this date (inclusive, YYYY-MM-DD)",
    )
    p.add_argument(
        "--until",
        default=None,
        help="Only download emails before this date (exclusive, YYYY-MM-DD)",
    )
    p.add_argument(
        "--incremental",
        metavar="PREV_ARCHIVE",
        default=None,
        help="Incremental backup: read manifest from PREV_ARCHIVE, download only new emails",
    )
    p.add_argument(
        "-o", "--output",
        default="emails.tar.zst",
        help="Output file path (default: emails.tar.zst)",
    )
    p.add_argument(
        "-t", "--threads",
        type=int,
        default=4,
        help="Number of parallel download threads (default: 4)",
    )
    args = p.parse_args()
    # Validate and parse date arguments
    for attr in ("since", "until"):
        val = getattr(args, attr)
        if val is not None:
            try:
                setattr(args, attr, datetime.date.fromisoformat(val))
            except ValueError:
                p.error(f"--{attr} must be in YYYY-MM-DD format, got '{val}'")
    return args


MANIFEST_NAME = "_manifest.json"


def get_uidvalidity(conn):
    """Return the UIDVALIDITY of the currently selected folder."""
    # After SELECT, the server sends an untagged OK response with UIDVALIDITY.
    # imaplib stores untagged responses in conn.untagged_responses.
    resp = conn.response("UIDVALIDITY")
    if resp and resp[1] and resp[1][0]:
        return int(resp[1][0])
    return None


def read_manifest(archive_path):
    """Extract and parse _manifest.json from a tar.zst archive.

    Returns the parsed dict, or None if the archive doesn't exist or
    has no manifest.
    """
    if not os.path.isfile(archive_path):
        return None
    try:
        result = subprocess.run(
            ["tar", "--zstd", "-xf", archive_path, "-O", MANIFEST_NAME],
            capture_output=True,
        )
        if result.returncode != 0:
            # Try pipe fallback (macOS BSD tar may not support --zstd)
            zstd = subprocess.Popen(
                ["zstd", "-dq"],
                stdin=open(archive_path, "rb"),
                stdout=subprocess.PIPE,
            )
            result = subprocess.run(
                ["tar", "-xf", "-", "-O", MANIFEST_NAME],
                stdin=zstd.stdout,
                capture_output=True,
            )
            zstd.wait()
            if result.returncode != 0:
                return None
        return json.loads(result.stdout)
    except Exception as e:
        log.warning("Could not read manifest from %s: %s", archive_path, e)
        return None


def write_manifest(tmpdir, manifest):
    """Write _manifest.json to the temp directory."""
    path = os.path.join(tmpdir, MANIFEST_NAME)
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)


def build_search_criteria(since, until):
    """Build IMAP SEARCH criteria from date filters.

    IMAP dates use "DD-Mon-YYYY" format (e.g. "01-Jan-2024").
    SINCE is inclusive (on or after), BEFORE is exclusive (strictly before).
    """
    # IMAP month abbreviations (1-indexed)
    _MONTHS = [
        "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]

    def fmt(d):
        return f"{d.day:d}-{_MONTHS[d.month]}-{d.year:04d}"

    parts = []
    if since is not None:
        parts.append(f"SINCE {fmt(since)}")
    if until is not None:
        parts.append(f"BEFORE {fmt(until)}")
    if not parts:
        return "ALL"
    return " ".join(parts)


def prompt_missing(args):
    """Prompt for any required values not provided via args or env."""
    # When stdin is a pipe, consume the password line first (before any
    # input() calls that would also read from the pipe and collide).
    piped_password = None
    if not args.password and not sys.stdin.isatty():
        piped_password = sys.stdin.readline().rstrip("\n")
        # Reopen stdin from the terminal so interactive prompts still work
        # (e.g. if host/user were not provided via args or env).
        try:
            sys.stdin = open("/dev/tty", "r")
        except OSError:
            pass  # no controlling terminal (e.g. cron) — prompts will fail

    if not args.host:
        args.host = input("IMAP host: ").strip()
    if args.port is None:
        env_port = os.environ.get("IMAP_PORT")
        if env_port:
            args.port = int(env_port)
        else:
            args.port = 993 if args.security == "ssl" else 143
    if not args.user:
        args.user = input("Username: ").strip()
    if not args.password:
        if piped_password is not None:
            args.password = piped_password
        else:
            args.password = getpass.getpass("Password: ")


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    args = parse_args()
    prompt_missing(args)

    # --- Enumerate folders and UIDs ---
    print(f"Connecting to {args.host}:{args.port} ({args.security})...")
    try:
        conn = connect(args.host, args.port, args.security)
        conn.login(args.user, args.password)
    except Exception as e:
        sys.exit(f"Connection failed: {e}")

    folders = list_folders(conn)
    print(f"Found {len(folders)} folder(s)")

    search_criteria = build_search_criteria(args.since, args.until)
    if search_criteria != "ALL":
        print(f"Date filter: {search_criteria}")

    # Load previous manifest for incremental mode
    prev_manifest = None
    if args.incremental:
        prev_manifest = read_manifest(args.incremental)
        if prev_manifest:
            prev_folders = prev_manifest.get("folders", {})
            print(f"Incremental: loaded manifest from {args.incremental}"
                  f" ({len(prev_folders)} folder(s))")
        else:
            print(f"Incremental: no manifest found in {args.incremental},"
                  " doing full download")

    work_items = []
    folder_meta = {}  # raw_name -> {uidvalidity, max_uid}

    for raw_name, display_name in folders:
        try:
            status, data = conn.select(quote_folder(raw_name), readonly=True)
            if status != "OK":
                log.warning("Could not select folder %s", display_name)
                continue
            uidvalidity = get_uidvalidity(conn)

            # Determine search criteria for this folder
            folder_search = search_criteria
            skipped_reason = None

            if prev_manifest and raw_name in prev_manifest.get("folders", {}):
                prev = prev_manifest["folders"][raw_name]
                prev_uidval = prev.get("uidvalidity")
                prev_max_uid = prev.get("max_uid", 0)

                if prev_uidval is not None and prev_uidval != uidvalidity:
                    skipped_reason = "UIDVALIDITY changed, full re-download"
                elif prev_max_uid > 0:
                    # Combine with any existing date filter
                    uid_filter = f"UID {prev_max_uid + 1}:*"
                    if folder_search == "ALL":
                        folder_search = uid_filter
                    else:
                        folder_search = f"{folder_search} {uid_filter}"

            status, data = conn.uid("search", None, folder_search)
            if status == "OK" and data[0]:
                uids = data[0].split()
                # Filter out UIDs <= prev_max_uid (some servers return the
                # boundary UID even when asked for UID max+1:*)
                if prev_manifest and raw_name in prev_manifest.get("folders", {}):
                    prev_max = prev_manifest["folders"][raw_name].get("max_uid", 0)
                    prev_uidval = prev_manifest["folders"][raw_name].get("uidvalidity")
                    if prev_uidval == uidvalidity and prev_max > 0:
                        uids = [u for u in uids if int(u) > prev_max]
                work_items.extend((raw_name, uid) for uid in uids)
                label = f"{len(uids)} emails"
                if skipped_reason:
                    label += f" ({skipped_reason})"
                elif prev_manifest and folder_search != search_criteria:
                    label += " (new)"
                print(f"  {display_name}: {label}")
            else:
                print(f"  {display_name}: 0 emails")

            # Record folder metadata for the new manifest
            # max_uid = max of what we download now + what was in prev manifest
            all_uids_int = [int(u) for _, u in work_items if _ == raw_name]
            prev_max = 0
            if prev_manifest and raw_name in prev_manifest.get("folders", {}):
                prev_info = prev_manifest["folders"][raw_name]
                if prev_info.get("uidvalidity") == uidvalidity:
                    prev_max = prev_info.get("max_uid", 0)
            cur_max = max(all_uids_int) if all_uids_int else 0
            folder_meta[raw_name] = {
                "uidvalidity": uidvalidity,
                "max_uid": max(cur_max, prev_max),
            }

        except Exception as e:
            log.warning("Could not list folder %s: %s", display_name, e)

    try:
        conn.logout()
    except Exception:
        pass

    if not work_items:
        print("No new emails found.")
        return

    total = len(work_items)
    print(f"\nDownloading {total} emails with {args.threads} thread(s)...\n")

    # Sort by folder to minimize SELECT commands across threads
    work_items.sort(key=lambda x: x[0])

    # Download to temp directory, then compress
    tmpdir = tempfile.mkdtemp(prefix="getmail_")
    try:
        pbar = ProgressBar(total, desc="Downloading")
        with ThreadPoolExecutor(max_workers=args.threads) as executor:
            futures = {
                executor.submit(
                    download_one,
                    folder, uid,
                    args.host, args.port, args.security,
                    args.user, args.password,
                    tmpdir,
                ): (folder, uid)
                for folder, uid in work_items
            }
            for future in as_completed(futures):
                try:
                    ok = future.result()
                    pbar.update(error=not ok)
                except Exception as e:
                    pbar.update(error=True)
                    log.warning("Download error: %s", e)
        pbar.finish()

        # Write manifest for future incremental backups
        manifest = {
            "host": args.host,
            "user": args.user,
            "date": datetime.datetime.now().isoformat(timespec="seconds"),
            "folders": folder_meta,
        }
        if args.incremental:
            manifest["incremental_from"] = os.path.basename(args.incremental)
        write_manifest(tmpdir, manifest)

        # Compress
        output_dir = os.path.dirname(os.path.abspath(args.output))
        os.makedirs(output_dir, exist_ok=True)

        print("Compressing...")
        compress(tmpdir, args.output)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    size_mb = os.path.getsize(args.output) / (1024 * 1024)
    errors = pbar.errors
    print(f"Done! Saved to {args.output} ({size_mb:.1f} MB)")
    if errors:
        print(f"  {errors} email(s) failed to download")
    print(f"  {total - errors}/{total} emails archived")


if __name__ == "__main__":
    main()
