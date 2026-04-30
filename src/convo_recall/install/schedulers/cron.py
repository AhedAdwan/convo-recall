"""CronScheduler — Linux fallback when systemd-user is unavailable.

Adds `@reboot` lines to the user crontab that spawn `recall watch` and
`recall serve` at boot. Each line we own ends with the suffix
`  # convo-recall:<purpose>` (two leading spaces) — that exact suffix is
the round-trip identifier on uninstall, so substring presence of
`convo-recall` elsewhere in a user's line is preserved.

Backup: the pre-modification crontab is written to
`<runtime_dir>/crontab.bak.<unix-ts>` before any change. `crontab -`
otherwise overwrites the entire file, so the merge-and-replace flow
must always backup first.
"""

import subprocess
import time
from pathlib import Path

from .._paths import runtime_dir
from .base import Result, Scheduler


_TAG_WATCH = "  # convo-recall:watch"
_TAG_EMBED = "  # convo-recall:embed"


class CronScheduler(Scheduler):
    def available(self) -> bool:
        try:
            r = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True,
            )
        except FileNotFoundError:
            return False
        # Exit 0 = existing crontab; exit 1 with "no crontab" = empty but usable.
        if r.returncode == 0:
            return True
        if r.returncode == 1 and "no crontab" in (r.stderr or "").lower():
            return True
        return False

    def describe(self) -> str:
        return "cron (Linux fallback)"

    def consequence_yes(self) -> str:
        return ("Cron `@reboot` line spawns `recall watch` at boot. "
                "Polling-based; reaction time depends on `recall watch`'s 10s tick.")

    def consequence_no(self) -> str:
        return "Indexing won't run automatically; use `recall ingest` manually."

    # ── Public ABC surface ────────────────────────────────────────────────────

    def install_watcher(
        self,
        agent: str,
        recall_bin: str,
        watch_dir: str,
        db_path: str,
        sock_path: str,
        config_path: str,
        log_dir: str,
    ) -> Result:
        line = (
            f"@reboot {recall_bin} watch >> {log_dir}/watch.log 2>&1{_TAG_WATCH}"
        )
        return self._append_tagged(line, _TAG_WATCH,
                                   already_msg="watcher already covered by polling cron line")

    def install_sidecar(
        self, recall_bin: str, sock_path: str, log_dir: str
    ) -> Result:
        line = (
            f"@reboot nohup {recall_bin} serve --sock {sock_path} "
            f"> {log_dir}/embed.log 2>&1 &{_TAG_EMBED}"
        )
        return self._append_tagged(line, _TAG_EMBED,
                                   already_msg="sidecar already covered by cron line")

    def uninstall_watcher(self, agent: str) -> Result:
        return self._remove_tagged(_TAG_WATCH, kind="watcher")

    def uninstall_sidecar(self) -> Result:
        return self._remove_tagged(_TAG_EMBED, kind="sidecar")

    # ── Internals ─────────────────────────────────────────────────────────────

    def _read_crontab(self) -> str:
        try:
            r = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True,
            )
        except FileNotFoundError:
            return ""
        if r.returncode == 0:
            return r.stdout
        # exit 1 + "no crontab" = empty-but-usable
        return ""

    def _write_crontab(self, content: str) -> Result:
        r = subprocess.run(
            ["crontab", "-"], input=content,
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return Result(ok=False, message=f"crontab write failed: {r.stderr.strip()}")
        return Result(ok=True, message="crontab updated")

    def _backup(self, content: str) -> Path:
        bak_dir = runtime_dir()
        bak_dir.mkdir(parents=True, exist_ok=True)
        bak_path = bak_dir / f"crontab.bak.{int(time.time())}"
        bak_path.write_text(content)
        return bak_path

    def _append_tagged(self, line: str, tag: str, already_msg: str) -> Result:
        existing = self._read_crontab()
        # Idempotency: any existing line ending in our tag suffix wins.
        for existing_line in existing.splitlines():
            if existing_line.rstrip("\n").rstrip().endswith(tag.strip()):
                return Result(ok=True, message=already_msg)

        bak_path = self._backup(existing)
        new_content = existing
        if new_content and not new_content.endswith("\n"):
            new_content += "\n"
        new_content += line + "\n"

        write = self._write_crontab(new_content)
        if not write.ok:
            return write
        return Result(ok=True, message=f"crontab line added (backup: {bak_path.name})",
                      path=bak_path)

    def _remove_tagged(self, tag: str, kind: str) -> Result:
        existing = self._read_crontab()
        kept_lines = []
        removed = 0
        for existing_line in existing.splitlines():
            if existing_line.rstrip("\n").rstrip().endswith(tag.strip()):
                removed += 1
                continue
            kept_lines.append(existing_line)

        if removed == 0:
            return Result(ok=True, message=f"no {kind} line found in crontab")

        bak_path = self._backup(existing)
        new_content = ""
        if kept_lines:
            new_content = "\n".join(kept_lines) + "\n"

        write = self._write_crontab(new_content)
        if not write.ok:
            return write
        return Result(ok=True, message=f"{kind} line removed (backup: {bak_path.name})",
                      path=bak_path)
