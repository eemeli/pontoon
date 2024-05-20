import logging
from os.path import relpath

from django.utils import timezone

from pontoon.base.models import Locale, Project
from pontoon.sync.checkout import get_checkouts
from pontoon.sync.get_paths import get_paths
from pontoon.sync.models import ProjectSyncLog, RepositorySyncLog, SyncLog
from pontoon.sync.sync_entities_from_repo import sync_entities_from_repo
from pontoon.sync.sync_translations_from_repo import sync_translations_from_repo

log = logging.getLogger(__name__)


def sync_project(
    project_pk: int,
    sync_log_pk: int,
    pull: bool = True,
    commit: bool = True,
    force: bool = False,
):
    try:
        project = Project.objects.get(pk=project_pk)
        sync_log = SyncLog.objects.get(pk=sync_log_pk)
    except Project.DoesNotExist:
        log.error(f"Could not sync project with pk={project_pk}, not found.")
        raise
    except SyncLog.DoesNotExist:
        log.error(
            f"Could not sync project {project.slug}, log with pk={sync_log_pk} not found."
        )
        raise

    # Mark "now" at the start of sync to avoid messing with
    # translations submitted during sync.
    now = timezone.now()

    log.info(f"Syncing project {project.slug}.")
    project_sync_log = ProjectSyncLog.objects.create(
        sync_log=sync_log, project=project, start_time=now
    )

    checkouts = list(get_checkouts(project, pull))
    if not checkouts:
        project_sync_log.skip()
        raise Exception(f"No repositories found for {project.slug}")
    repo_sync_log = RepositorySyncLog.objects.create(
        project_sync_log=project_sync_log,
        repository=next(
            (co.repo for co in checkouts if not co.is_source), checkouts[0].repo
        ),
        start_time=timezone.now(),
    )

    paths, ref_checkout = get_paths(project, checkouts)
    locale_map: dict[str, Locale] = {
        lc.code: lc for lc in project.locales.order_by("code")
    }
    paths.locales = list(locale_map.keys())
    added, changed, removed = sync_entities_from_repo(
        project,
        locale_map,
        paths,
        relpath(paths.ref_root, ref_checkout.path),
        ref_checkout.changed,
        ref_checkout.removed,
        now,
    )
    # TODO: send notifications
    sync_translations_from_repo(
        project, locale_map, checkouts, ref_checkout, paths, now
    )
    # have_repos_changed = any(co.commit is None or co.commit != co.prev_commit for co in checkouts)
    # pulled_revisions = {co.locale_code or "single_locale": co.commit for co in checkouts}
    repo_sync_log.end()
