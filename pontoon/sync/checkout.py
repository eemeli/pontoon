import logging
from collections.abc import Iterator
from os import walk
from os.path import join, normpath, relpath
from typing import cast

from django.db.models import BaseManager

from pontoon.base.models import Locale, Project, Repository
from pontoon.sync.repositories import get_repo

log = logging.getLogger(__name__)


class Checkout:
    locale_code: str | None
    """Set for multi-locale repositories"""
    repo: Repository
    is_source: bool
    url: str
    path: str
    prev_commit: str | None
    commit: str | None
    changed: list[str]
    """Relative paths from the checkout base"""
    removed: list[str]
    """Relative paths from the checkout base"""

    def __init__(
        self, db_repo: Repository, locale_code: str | None, pull: bool
    ) -> None:
        self.locale_code = locale_code
        self.repo = db_repo
        if locale_code is None:
            self.is_source = db_repo.source_repo
            self.url = db_repo.url
            self.path = normpath(db_repo.checkout_path)
        else:
            self.is_source = False
            self.url = db_repo.url.format(locale_code=locale_code)
            self.path = normpath(join(db_repo.checkout_path, locale_code))

        if db_repo.last_synced_revisions is None:
            self.prev_commit = None
        else:
            pc = db_repo.last_synced_revisions.get(
                self.locale_code or "single_locale", None
            )
            self.prev_commit = pc if isinstance(pc, str) else None

        versioncontrol = get_repo(db_repo.type)
        if pull:
            log.info(f"Pulling updates from {self.url}")
            versioncontrol.update(self.url, self.path, db_repo.branch)
        self.commit = versioncontrol.revision(self.path)

        delta = (
            versioncontrol.changed_files(self.path, self.prev_commit)
            if isinstance(self.prev_commit, str)
            else None
        )
        if delta is not None:
            self.changed, self.removed = delta
        else:
            # Initially and on error, consider all files changed
            self.changed = []
            for root, dirnames, filenames in walk(self.path):
                dirnames[:] = (dn for dn in dirnames if not dn.startswith("."))
                rel_root = relpath(root, self.path) if root != self.path else ""
                self.changed.extend(
                    join(rel_root, fn) for fn in filenames if not fn.startswith(".")
                )
            self.removed = []


def get_checkouts(project: Project, pull: bool = True) -> Iterator[Checkout]:
    """
    For each project repository including all multi-locale repositories,
    update its local checkout (unless `pull` is false),
    and provide a `Checkout` representing their current state.
    """
    for repo in cast(BaseManager[Repository], project.repositories).all():
        if "{locale_code}" in repo.url:
            for locale_code in cast(BaseManager[Locale], project.locales).values_list(
                "code", flat=True
            ):
                yield Checkout(repo, locale_code, pull)
        else:
            yield Checkout(repo, None, pull)
