import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path, PureWindowsPath

CHUNK_SIZE = 256 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
PARTIAL_EXTENSIONS = (".crdownload", ".tmp", ".part")
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
DOWNLOADS_FOLDER_GUID = "{374DE290-123F-4565-9164-39C4925E467B}"


def brave_user_data_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "BraveSoftware" / "Brave-Browser" / "User Data"


def read_brave_preferences(profile: str) -> dict:
    try:
        with open(brave_user_data_dir() / profile / "Preferences", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, KeyError):
        return {}


def windows_downloads_dir() -> Path:
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, DOWNLOADS_FOLDER_GUID)
        return Path(os.path.expandvars(value))
    except (ImportError, OSError):
        return Path.home() / "Downloads"


def brave_downloads_dir(preferences: dict) -> Path:
    configured = preferences.get("download", {}).get("default_directory")
    return Path(configured) if configured else windows_downloads_dir()


def find_brave_executable() -> str:
    candidates = []
    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\brave.exe"
                with winreg.OpenKey(root, key_path) as key:
                    candidates.append(Path(winreg.QueryValue(key, None)))
            except OSError:
                pass
    except ImportError:
        pass
    candidates += [
        Path(os.environ.get(var, "")) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
    ]
    if shutil.which("brave"):
        candidates.append(Path(shutil.which("brave")))

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    sys.exit("[ERRORE] brave.exe non trovato. Brave e' installato?")


def chrome_time_to_str(microseconds: int) -> str:
    if not microseconds:
        return "?"
    dt = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=microseconds)
    return dt.astimezone().strftime("%d/%m/%Y %H:%M")


def read_download_history(history_file: Path) -> list:
    if not history_file.exists():
        sys.exit(f"[ERRORE] File cronologia non trovato: {history_file}\n"
                 f"         Controlla il profilo con --profile oppure indica il file con --history.")

    tmp_dir = Path(tempfile.mkdtemp(prefix="brave_history_"))
    tmp_db = tmp_dir / "History"
    try:
        try:
            shutil.copy2(history_file, tmp_db)
        except OSError:
            sys.exit("[ERRORE] Impossibile leggere la cronologia: chiudi Brave e riprova.")

        conn = sqlite3.connect(str(tmp_db))
        try:
            rows = conn.execute(
                """
                SELECT d.id,
                       d.start_time,
                       d.target_path,
                       d.referrer,
                       (SELECT url FROM downloads_url_chains
                         WHERE id = d.id ORDER BY chain_index ASC  LIMIT 1) AS original_url,
                       (SELECT url FROM downloads_url_chains
                         WHERE id = d.id ORDER BY chain_index DESC LIMIT 1) AS final_url
                  FROM downloads d
                 ORDER BY d.start_time ASC
                """
            ).fetchall()
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    downloads = []
    seen = set()
    for dl_id, start_time, target_path, referrer, original_url, final_url in rows:
        url = original_url or final_url
        if not url or urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            continue
        if url in seen:
            continue
        seen.add(url)
        downloads.append({
            "id": dl_id,
            "date": chrome_time_to_str(start_time),
            "name": PureWindowsPath(target_path or "").name,
            "referrer": referrer or None,
            "url": url,
            "final_url": final_url if final_url != url else None,
        })
    return downloads


def sanitize_filename(name: str) -> str:
    name = INVALID_FILENAME_CHARS.sub("_", name).strip(" .")
    if name.split(".")[0].upper() in RESERVED_NAMES:
        name = "_" + name
    return name[:200] or "download"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def filename_from_response(resp) -> str:
    header = resp.headers.get("Content-Disposition")
    if header:
        msg = Message()
        msg["Content-Disposition"] = header
        name = msg.get_filename()
        if name:
            return name
    return urllib.parse.unquote(Path(urllib.parse.urlsplit(resp.geturl()).path).name)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def print_status(text: str) -> None:
    width = shutil.get_terminal_size((80, 25)).columns - 1
    print(f"\r{text[:width]:<{width}}", end="", flush=True)


def print_progress(done: int, total: int, started: float) -> None:
    speed = done / max(time.monotonic() - started, 0.001)
    if total:
        pct = done / total
        bar = "#" * int(pct * 25)
        print_status(f"  [{bar:<25}] {pct:6.1%}  {human_size(done)} / {human_size(total)}  {human_size(speed)}/s")
    else:
        print_status(f"  {human_size(done)} scaricati  {human_size(speed)}/s")


def download_file(url: str, referrer, dest_dir: Path, preferred_name: str, timeout: int) -> Path:
    headers = {"User-Agent": USER_AGENT}
    if referrer:
        headers["Referer"] = referrer
    request = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(request, timeout=timeout) as resp:
        name = preferred_name or filename_from_response(resp) or "download"
        dest = unique_path(dest_dir / sanitize_filename(name))
        partial = dest.with_name(dest.name + ".part")
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        started = time.monotonic()
        try:
            with open(partial, "wb") as f:
                while True:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    print_progress(done, total, started)
            if total and done < total:
                raise IOError(f"download incompleto ({done} di {total} byte)")
            os.replace(partial, dest)
        except BaseException:
            print()
            partial.unlink(missing_ok=True)
            raise
    print()
    return dest


def list_dir(folder: Path) -> set:
    return {p.name for p in folder.iterdir()}


def is_partial(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(PARTIAL_EXTENSIONS) or lower.startswith("unconfirmed ")


def download_with_brave(brave: str, url: str, downloads_dir: Path, start_timeout: int, timeout: int) -> list:
    before = list_dir(downloads_dir)
    subprocess.Popen([brave, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.monotonic() + start_timeout
    while not (list_dir(downloads_dir) - before):
        if time.monotonic() > deadline:
            raise TimeoutError(f"nessun download partito entro {start_timeout}s")
        time.sleep(1)

    deadline = time.monotonic() + timeout
    stable_checks = 0
    started = time.monotonic()
    while stable_checks < 3:
        if time.monotonic() > deadline:
            raise TimeoutError(f"download non completato entro {timeout}s")
        new_files = list_dir(downloads_dir) - before
        stable_checks = stable_checks + 1 if new_files and not any(map(is_partial, new_files)) else 0
        print_status(f"  in attesa che Brave finisca... {int(time.monotonic() - started)}s")
        time.sleep(1)
    print()
    return sorted(new_files)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Riscarica in sequenza i download presenti nella cronologia di Brave.",
        epilog="esempi:\n"
               "  py brave_reDownload.py --list\n"
               "  py brave_reDownload.py --limit 5 --output D:\\dl\n"
               "  py brave_reDownload.py --brave",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--profile", default="Default", help='profilo Brave (es. "Default", "Profile 1")')
    parser.add_argument("--history", type=Path, help="percorso esplicito del file History")
    parser.add_argument("--output", type=Path, default=Path("riscaricati"), help="cartella di destinazione")
    parser.add_argument("--limit", type=int, help="riscarica solo gli ultimi N download")
    parser.add_argument("--list", action="store_true", help="mostra i link senza scaricare")
    parser.add_argument("--brave", action="store_true", help="apri i link in Brave invece di scaricarli con Python")
    parser.add_argument("--brave-dir", type=Path,
                        help="cartella Download di Brave (solo con --brave, di solito la trova da solo)")
    parser.add_argument("--timeout", type=int, default=3600, help="secondi massimi per ogni download")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = parse_args()

    history_file = args.history or brave_user_data_dir() / args.profile / "History"
    downloads = read_download_history(history_file)
    if args.limit:
        downloads = downloads[-args.limit:]

    if not downloads:
        print("Nessun link scaricabile trovato nella cronologia download.")
        return 0

    print(f"Trovati {len(downloads)} link nella cronologia ({history_file})\n")
    if args.list:
        for i, dl in enumerate(downloads, 1):
            print(f"{i:>3}. [{dl['date']}] {dl['name'] or '(senza nome)'}\n     {dl['url']}")
        return 0

    if args.brave:
        brave = find_brave_executable()
        preferences = read_brave_preferences(args.profile)
        brave_dir = args.brave_dir or brave_downloads_dir(preferences)
        if not brave_dir.is_dir():
            sys.exit(f"[ERRORE] Cartella Download di Brave non trovata: {brave_dir}\n"
                     f"         Indicala con --brave-dir.")
        print(f"Cartella Download di Brave: {brave_dir}\n")
        if preferences.get("download", {}).get("prompt_for_download"):
            print("[ATTENZIONE] Brave chiede dove salvare ogni file: dovrai confermare tu ogni download.\n"
                  "             Per evitarlo: Impostazioni > Download > disattiva \"Chiedi dove salvare\".\n")
    else:
        args.output.mkdir(parents=True, exist_ok=True)
        print(f"Destinazione: {args.output.resolve()}\n")

    ok, failed = [], []
    try:
        for i, dl in enumerate(downloads, 1):
            print(f"[{i}/{len(downloads)}] {dl['name'] or dl['url']}")
            print(f"  {dl['url']}")
            try:
                if args.brave:
                    files = download_with_brave(brave, dl["url"], brave_dir, 60, args.timeout)
                    print(f"  OK -> {', '.join(files)}")
                else:
                    try:
                        dest = download_file(dl["url"], dl["referrer"], args.output, dl["name"], args.timeout)
                    except (urllib.error.URLError, OSError) as first_error:
                        if not dl["final_url"]:
                            raise
                        print(f"  link originale fallito ({first_error}), provo l'URL finale...")
                        dest = download_file(dl["final_url"], dl["referrer"], args.output, dl["name"], args.timeout)
                    print(f"  OK -> {dest.name}")
                ok.append(dl)
            except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
                print(f"  FALLITO: {e}")
                failed.append((dl, e))
            print()
    except KeyboardInterrupt:
        print("\n\nInterrotto dall'utente (Ctrl+C).")

    print("=" * 60)
    print(f"Completati: {len(ok)}   Falliti: {len(failed)}   Totale: {len(downloads)}")
    for dl, e in failed:
        print(f"  - {dl['name'] or dl['url']}: {e}")
    return 1 if failed else 0


def pause_if_double_clicked() -> None:
    if sys.platform == "win32" and "PROMPT" not in os.environ and sys.stdin and sys.stdin.isatty():
        try:
            input("\nPremi Invio per chiudere...")
        except (EOFError, KeyboardInterrupt):
            pass


if __name__ == "__main__":
    try:
        exit_code = main()
    finally:
        pause_if_double_clicked()
    sys.exit(exit_code)
