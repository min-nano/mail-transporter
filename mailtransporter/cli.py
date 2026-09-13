"""Local helpers: ``python -m mailtransporter.cli sync`` runs one pass.

The maintenance commands exist because quarantined mail is marked with a
custom IMAP keyword rather than moved into a folder: Apple Mail and
icloud.com do not show custom keywords, so this is the way to see what was
set aside and to put it back into the queue.
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import ICloudSettings, Keywords, keywords_from_env
from .imap_client import ICloudMailbox, MailboxError
from .runtime import build_forwarder, configure_logging
from .secrets import ConfigError


def _mailbox(*, readonly: bool) -> tuple[ICloudMailbox, Keywords]:
    """An iCloud session plus the keywords this tool writes on messages.

    Deliberately independent of the Gmail settings: looking at or re-queuing
    quarantined mail must work without OAuth credentials in the environment.
    """
    icloud = ICloudSettings.from_env()
    keywords = keywords_from_env()
    mailbox = ICloudMailbox(
        icloud.host,
        icloud.port,
        icloud.user,
        icloud.password,
        timeout=icloud.timeout,
        readonly=readonly,
        skip_keywords=(keywords.failed, keywords.unverified),
    )
    return mailbox, keywords


def _collect(mailbox: ICloudMailbox, keywords: Keywords) -> list[dict]:
    entries: list[dict] = []
    for uid, flags in mailbox.inbox_flags().items():
        lowered = {f.lower() for f in flags}
        keyword = next((k for k in (keywords.failed, keywords.unverified) if k.lower() in lowered), None)
        if keyword is None:
            continue
        # Whether Gmail has the message is decided by the inserted keyword, not
        # by which keyword quarantined it: a message marked failed by hand may
        # already carry it.
        in_gmail = keyword == keywords.unverified or keywords.inserted.lower() in lowered
        entries.append({"uid": uid, "keyword": keyword, "in_gmail": in_gmail})
    headers = mailbox.fetch_headers([entry["uid"] for entry in entries])
    for entry in entries:
        entry.update(headers.get(entry["uid"], {}))
    entries.sort(key=lambda entry: entry["uid"])
    return entries


def _cmd_list(args) -> int:
    mailbox, keywords = _mailbox(readonly=True)
    with mailbox:
        entries = _collect(mailbox, keywords)
    if args.json:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
        return 0
    if not entries:
        print("No quarantined messages.")
        return 0
    for entry in entries:
        where = "in Gmail (re-queuing duplicates it)" if entry["in_gmail"] else "not in Gmail"
        print(f"uid={entry['uid']} {entry['keyword']} [{where}]")
        print(f"    date:    {entry.get('date', '')}")
        print(f"    from:    {entry.get('from', '')}")
        print(f"    subject: {entry.get('subject', '')}")
    print(f"\n{len(entries)} message(s). Re-queue with: mailtransporter retry --uid <UID>")
    return 0


def _cmd_retry(args) -> int:
    if args.unverified and not args.yes:
        print(
            "Messages carrying the unverified keyword were already accepted by Gmail; re-queuing "
            "them inserts a duplicate. Pass --yes to confirm.",
            file=sys.stderr,
        )
        return 2
    mailbox, keywords = _mailbox(readonly=False)
    keyword = keywords.unverified if args.unverified else keywords.failed
    with mailbox:
        carrying = set(mailbox.search_keyword(keyword))
        uids = sorted(carrying) if args.all else list(args.uid)
        if not uids:
            print(f"No message carries {keyword}.")
            return 0
        for uid in uids:
            # Clearing a keyword the message does not have would silently
            # report success while it stays quarantined under the other one.
            if uid not in carrying:
                print(f"uid={uid} does not carry {keyword}; skipped", file=sys.stderr)
                continue
            mailbox.remove_keyword(uid, keyword)
            print(f"uid={uid} {keyword} cleared; it will be picked up by the next run")
    return 0


def _cmd_mark_failed(args) -> int:
    mailbox, keywords = _mailbox(readonly=False)
    refused = False
    with mailbox:
        mailbox.require_keywords()
        flags = mailbox.inbox_flags()
        for uid in args.uid:
            # "Failed" means Gmail never got it. Marking an already inserted
            # message that way would misreport it, and it only needs trashing.
            if keywords.inserted.lower() in {f.lower() for f in flags.get(uid, ())}:
                print(
                    f"uid={uid} already carries {keywords.inserted} (Gmail has it); "
                    "not marking it as failed",
                    file=sys.stderr,
                )
                refused = True
                continue
            mailbox.add_keyword(uid, keywords.failed)
            print(f"uid={uid} marked {keywords.failed}; it will be skipped from now on")
    return 1 if refused else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mailtransporter")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="Run one forwarding pass with the current environment")

    listing = sub.add_parser(
        "list-quarantined", aliases=["list-failed"], help="Show the mail set aside by keyword"
    )
    listing.add_argument("--json", action="store_true", help="Machine-readable output")

    retry = sub.add_parser("retry", help="Clear the quarantine keyword so the mail is forwarded again")
    target = retry.add_mutually_exclusive_group(required=True)
    target.add_argument("--uid", type=int, nargs="+", default=[], help="UIDs to re-queue")
    target.add_argument("--all", action="store_true", help="Re-queue every quarantined message")
    retry.add_argument(
        "--unverified",
        action="store_true",
        help="Act on mail Gmail already accepted (this creates duplicates); needs --yes",
    )
    retry.add_argument("--yes", action="store_true", help="Confirm the duplicate-creating --unverified case")

    mark = sub.add_parser(
        "mark-failed", help="Take a message out of the queue by hand (e.g. one stuck on transient errors)"
    )
    mark.add_argument("--uid", type=int, nargs="+", required=True, help="UIDs to set aside")

    args = parser.parse_args(argv)
    configure_logging()
    try:
        if args.command == "sync":
            result = build_forwarder().run()
            print(json.dumps(result.to_dict(), indent=2, default=str))
            return 0 if result.ok else 1
        if args.command in ("list-quarantined", "list-failed"):
            return _cmd_list(args)
        if args.command == "retry":
            return _cmd_retry(args)
        if args.command == "mark-failed":
            return _cmd_mark_failed(args)
    except (MailboxError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
