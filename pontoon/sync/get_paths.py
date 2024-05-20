import logging
from os.path import commonpath, join

from moz.l10n.paths import L10nConfigPaths, L10nDiscoverPaths, get_android_locale

from pontoon.base.models import Project
from pontoon.sync.checkout import Checkout
from pontoon.sync.vcs.project import (
    MissingLocaleDirectoryError,
    MissingSourceDirectoryError,
)

log = logging.getLogger(__name__)


def get_paths(
    project: Project, checkouts: list[Checkout]
) -> tuple[L10nConfigPaths | L10nDiscoverPaths, Checkout]:
    ref_checkout = next((co for co in checkouts if co.is_source), None)
    if project.configuration_file:
        if ref_checkout is None:
            try:
                (ref_checkout,) = (co for co in checkouts if co.locale_code is None)
            except ValueError:
                log.error(
                    f"Could not sync project {project.slug}, source repo not found."
                )
                raise MissingSourceDirectoryError
        paths = L10nConfigPaths(
            join(ref_checkout.path, project.configuration_file),
            locale_map={"android_locale": get_android_locale},
        )
        if len(checkouts) > 1:
            try:
                (target_repo,) = set(co.repo for co in checkouts if co != ref_checkout)
            except ValueError:
                log.error("Multiple target repos are not supported")
                raise MissingLocaleDirectoryError
            paths.base = target_repo.checkout_path
        return paths, ref_checkout
    else:
        ref_root = None if ref_checkout is None else ref_checkout.path
        paths = L10nDiscoverPaths(project.checkout_path, ref_root)
        if ref_checkout is None:
            ref_checkout = next(
                co
                for co in checkouts
                if commonpath((co.path, paths.ref_root)) == co.path
            )
        if paths.base is None:
            log.error(f"Base localization directory not found for {project.slug}")
            raise MissingLocaleDirectoryError
        return paths, ref_checkout
